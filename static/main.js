/* ═══════════════════════════════════════════════════════════
   ReWrite Master — Pipeline JS
═══════════════════════════════════════════════════════════ */

// ─── State ────────────────────────────────────────────────
let P = window.PROJECT_DATA;           // live project state
const PID = window.PROJECT_ID;
let isRunning = false;
let stopRequested = false;
let saveTimer = null;
let _didAutoScroll = false;
let activeAbortController = null;
let activeStage = null;
const locks = {
  master: true,
  pre_analysis: true,
  analysis: true,
  structure: true,
  block_writer: true,
  merger: true,
  quality_check: true,
  final: true,
  humanize_tts: true,
  scene_builder: true,
};
const collapsed = {
  pre_analysis: false,
  analysis: false,
  structure: false,
  block_writer: false,
  merger: false,
  quality_check: false,
  final: false,
  humanize_tts: false,
  scene_builder: false,
};

// ─── Init ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  renderAll();
  initSplitter();
  initResizableTextareas();
  setupEventListeners();
  log('Проект загружен', 'info');
});

function initSplitter() {
  const splitter = $('splitter');
  const layout = document.querySelector('.project-layout');
  if (!splitter || !layout) return;

  const minW = 260;
  const maxW = 520;
  let dragging = false;

  const apply = (w) => {
    const clamped = Math.max(minW, Math.min(maxW, w));
    document.documentElement.style.setProperty('--settings-w', `${clamped}px`);
    try { localStorage.setItem('rw_settings_w', String(clamped)); } catch (_) {}
  };

  try {
    const saved = parseInt(localStorage.getItem('rw_settings_w') || '', 10);
    if (saved) apply(saved);
  } catch (_) {}

  splitter.addEventListener('mousedown', (e) => {
    e.preventDefault();
    dragging = true;
    document.body.style.cursor = 'col-resize';
  });

  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const rect = layout.getBoundingClientRect();
    apply(e.clientX - rect.left);
  });

  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = '';
  });
}

// ─── Resizable textareas ──────────────────────────────────
// Persist heights per-project in localStorage, add expand button

const LS_KEY = () => `rw_heights_${PID}`;

// Auto-size textarea to exactly fit its content — no limits
// Auto-size only block-item textareas (they need full height, no scroll)
function autoSizeTextarea(ta) {
  if (!ta) return;
  ta.style.height = 'auto';
  ta.style.height = (ta.scrollHeight + 2) + 'px';
}

// Scroll result textarea to bottom during streaming
function scrollResultToBottom(ta) {
  if (ta) ta.scrollTop = ta.scrollHeight;
}

function autoSizeAll() {
  // prompt/result/medium: CSS handles height via max-height + overflow-y:auto + resize:vertical
  // Just scroll to top so content starts from beginning
  document.querySelectorAll('.prompt-textarea, .result-textarea, .textarea-medium')
    .forEach(ta => { ta.scrollTop = 0; });
}

function initResizableTextareas() {
  autoSizeAll();

  // Add collapse/expand toggle button to every result-label and prompt-label
  document.querySelectorAll('.result-label').forEach(label => {
    const ta = label.closest('.result-block')?.querySelector('textarea');
    if (!ta || ta.style.display === 'none') return;
    injectExpandBtn(label, ta);
  });
  document.querySelectorAll('.prompt-label').forEach(label => {
    const ta = label.closest('.prompt-block')?.querySelector('textarea');
    if (!ta) return;
    injectExpandBtn(label, ta, 'sm');
  });
}

function injectExpandBtn(label, ta, size = '') {
  if (label.querySelector('.btn-expand')) return;
  const btn = document.createElement('button');
  btn.className = `btn-expand btn-expand-${size || 'md'}`;
  btn.title = 'Свернуть';
  btn.innerHTML = '▲';
  btn.onclick = (e) => { e.stopPropagation(); toggleExpand(ta, btn); };
  label.appendChild(btn);
}

function toggleExpand(ta, btn) {
  const collapsed = ta.dataset.collapsed === '1';
  if (collapsed) {
    // Restore: auto-size to full content
    ta.dataset.collapsed = '0';
    ta.style.height = 'auto';
    autoSizeTextarea(ta);
    btn.innerHTML = '▲';
    btn.title = 'Свернуть';
  } else {
    // Collapse to 80px preview
    ta.dataset.collapsed = '1';
    ta.style.height = '80px';
    ta.style.overflowY = 'auto';
    btn.innerHTML = '▼';
    btn.title = 'Развернуть';
  }
}

function saveHeights() {
  const heights = {};
  document.querySelectorAll('textarea[id]').forEach(ta => {
    if (ta.style.height) heights[ta.id] = ta.style.height;
  });
  try { localStorage.setItem(LS_KEY(), JSON.stringify(heights)); } catch(_) {}
}

function restoreHeights() {
  try {
    const saved = JSON.parse(localStorage.getItem(LS_KEY()) || '{}');
    Object.entries(saved).forEach(([id, h]) => {
      const ta = document.getElementById(id);
      if (ta) ta.style.height = h;
    });
  } catch(_) {}
}

function ensureVisible(id) {
  const el = $(id);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function ensureRuntimeVisibility() {
  if (_didAutoScroll) return;
  _didAutoScroll = true;
  // Prefer Block Writer area (most “alive”), then log.
  ensureVisible('card-block_writer');
  setTimeout(() => ensureVisible('log-body'), 250);
}

function renderAll() {
  setTitle(P.name);

  $('source-text').value     = P.source_text || '';
  $('master-prompt').value   = P.master_prompt || '';
  $('hero-prompt').value     = P.hero_prompt || '';
  $('duration-slider').value = P.duration_minutes || 20;
  $('chars-per-min').value   = P.chars_per_minute || 700;
  $('humanize-mode').value   = P.humanize_mode || 'norm';
  $('voice-language-select').value = P.voice_language || 'ru';
  updateDurationDisplay();
  updateSourceCount();

  ['pre_analysis', 'analysis', 'structure', 'merger', 'quality_check', 'final', 'humanize_tts', 'scene_builder'].forEach(stage => {
    const s = P.stages[stage];
    if (!s) return;
    $(`prompt-${stage}`).value = s.prompt || '';
    $(`result-${stage}`).value = s.result || '';
    updateCount(stage);
    if (s.result) setBadge(stage, 'done');
  });

  // Scene Builder init
  const sds = P.scene_duration_seconds || 6;
  $('scene-duration-slider').value = sds;
  updateSceneDurationDisplay(sds);
  $('scene-style-prefix').value = P.scene_style_prefix || '';
  updateSceneBtnState();
  if (P.stages.scene_builder?.result) {
    updateSceneCount(P.stages.scene_builder.result);
    $('btn-export-scenes').style.display = 'inline-flex';
    $('btn-export-scenes-text').style.display = 'inline-flex';
  }

  // Block writer — render saved blocks as cards
  $('prompt-block_writer').value = P.stages.block_writer.prompt || '';
  const savedBlocks = P.stages.block_writer.blocks || [];
  if (savedBlocks.length) {
    renderBlockCards(savedBlocks);
    setBadge('block_writer', 'done');
  }

  // Block plan — show if structure is done
  if (P.stages.structure.result) renderBlockPlan();

  $('btn-export').href = `/project/${PID}/export`;
}

// ─── DOM helpers ──────────────────────────────────────────
function $(id) { return document.getElementById(id); }

function setTitle(name) {
  const d = $('project-name-display');
  if (d) { d.textContent = name; document.title = `${name} — ReWrite Master`; }
}


function updateDurationDisplay() {
  const min = parseInt($('duration-slider').value) || 20;
  const cpm = parseInt($('chars-per-min').value) || 700;
  $('duration-val').textContent = `${min} мин`;
  $('target-chars-display').textContent = (min * cpm).toLocaleString('ru-RU');
}

function updateSourceCount() {
  const len = ($('source-text').value || '').length;
  $('source-count').textContent = `${len.toLocaleString('ru-RU')} символов`;
}

function updateCount(stage) {
  const el  = $(`result-${stage}`);
  const cnt = $(`count-${stage}`);
  if (!el || !cnt) return;
  const text  = el.value || '';
  const len   = text.length;
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  cnt.textContent = `${len.toLocaleString('ru-RU')} симв / ${words.toLocaleString('ru-RU')} сл`;
}

// ─── Block Plan (editable target_chars table) ─────────────
function getStructure() {
  try {
    const parsed = JSON.parse(P.stages.structure.result || '{}');
    // Model sometimes returns a raw array instead of {"blocks": [...]}
    if (Array.isArray(parsed)) return { blocks: parsed };
    return parsed;
  } catch (_) { return {}; }
}

function getStructureBlocks() {
  return getStructure().blocks || [];
}

function planTarget() {
  return (parseInt(P.duration_minutes) || 20) * (parseInt(P.chars_per_minute) || 700);
}

function renderBlockPlan() {
  const container = $('block-plan-container');
  if (!container) return;

  const struct  = getStructure();
  const blocks  = struct.blocks || [];
  if (!blocks.length) { container.style.display = 'none'; return; }

  container.style.display = 'block';

  const writtenArr = P.stages.block_writer.blocks || [];
  const tbody = $('plan-tbody');
  if (!tbody) return;

  tbody.innerHTML = blocks.map((b, i) => {
    const written   = writtenArr[i] ? writtenArr[i].length : null;
    const target    = b.target_chars || 0;
    const delta     = written !== null ? written - target : null;
    const rowClass  = written !== null ? 'plan-row-done' : '';
    const deltaHtml = delta !== null
      ? `<span class="plan-delta-cell ${delta > 50 ? 'over' : delta < -50 ? 'under' : 'ok'}">${delta > 0 ? '+' : ''}${delta}</span>`
      : '<span class="plan-delta-cell">—</span>';
    const writtenHtml = written !== null
      ? written.toLocaleString('ru-RU')
      : '<span style="color:var(--text-d)">—</span>';

    return `<tr class="${rowClass}" id="plan-row-${i}">
      <td class="plan-num">${i + 1}</td>
      <td class="plan-name" title="${escHtml(b.block_name || '')}">${escHtml(b.block_name || b.title || `Блок ${i+1}`)}</td>
      <td class="plan-role">${escHtml(b.block_role || '')}</td>
      <td>
        <input class="plan-input" type="number" min="100" max="9999" step="50"
          value="${target}"
          id="plan-input-${i}"
          onchange="onPlanInputChange(${i}, this)"
          oninput="onPlanInputPreview(${i}, this)">
      </td>
      <td class="plan-written-cell" id="plan-written-${i}">${writtenHtml}</td>
      <td id="plan-delta-${i}">${deltaHtml}</td>
    </tr>`;
  }).join('');

  _updatePlanTotals(blocks, writtenArr);
}

function onPlanInputPreview(i, input) {
  // Live: mark changed, update sum preview
  const val = parseInt(input.value) || 0;
  input.classList.toggle('changed', true);
  _updatePlanTotalsFromInputs();
}

function onPlanInputChange(i, input) {
  const val = parseInt(input.value);
  if (!val || val < 100) { input.value = 100; }

  const struct = getStructure();
  if (!struct.blocks || !struct.blocks[i]) return;
  struct.blocks[i].target_chars = parseInt(input.value);
  input.classList.remove('changed');

  P.stages.structure.result = JSON.stringify(struct, null, 2);
  $('result-structure').value = P.stages.structure.result;
  updateCount('structure');

  _updatePlanTotalsFromInputs();
  scheduleSave();
  log(`📐 Блок ${i + 1}: цель изменена → ${parseInt(input.value).toLocaleString('ru-RU')} симв`);
}

function rebalanceBlocks() {
  const struct = getStructure();
  const blocks = struct.blocks || [];
  if (!blocks.length) return;

  const target = planTarget();

  // Read current values from inputs (user may have typed without confirming)
  blocks.forEach((b, i) => {
    const inp = $(`plan-input-${i}`);
    if (inp) b.target_chars = Math.max(100, parseInt(inp.value) || b.target_chars || 300);
  });

  const currentSum = blocks.reduce((s, b) => s + (b.target_chars || 0), 0);
  if (currentSum === 0) return;

  const ratio = target / currentSum;
  blocks.forEach(b => { b.target_chars = Math.max(100, Math.round((b.target_chars || 300) * ratio)); });

  // Fix rounding drift
  const adj = blocks.reduce((s, b) => s + b.target_chars, 0);
  blocks[blocks.length - 1].target_chars += (target - adj);
  if (blocks[blocks.length - 1].target_chars < 100) blocks[blocks.length - 1].target_chars = 100;

  struct.blocks = blocks;
  P.stages.structure.result = JSON.stringify(struct, null, 2);
  $('result-structure').value = P.stages.structure.result;
  updateCount('structure');

  scheduleSave();
  renderBlockPlan();
  log(`↺ Блоки выровнены: ${blocks.length} блоков × ≈${Math.round(target / blocks.length).toLocaleString('ru-RU')} симв = ${target.toLocaleString('ru-RU')}`);
}

function _updatePlanTotals(blocks, writtenArr) {
  const target      = planTarget();
  const sumTarget   = blocks.reduce((s, b) => s + (b.target_chars || 0), 0);
  const sumWritten  = (writtenArr || []).reduce((s, t) => s + (t ? t.length : 0), 0);
  const diff        = sumTarget - target;

  const sumEl = $('plan-sum');
  if (sumEl) {
    sumEl.textContent = `${sumTarget.toLocaleString('ru-RU')} / ${target.toLocaleString('ru-RU')}`;
    sumEl.className   = `plan-sum ${Math.abs(diff) <= 10 ? 'ok' : diff > 0 ? 'over' : 'under'}`;
  }
  const totTarget = $('plan-total-target');
  if (totTarget) totTarget.textContent = sumTarget.toLocaleString('ru-RU');

  const totWritten = $('plan-total-written');
  if (totWritten) totWritten.textContent = sumWritten ? sumWritten.toLocaleString('ru-RU') : '—';

  const totDelta = $('plan-total-delta');
  if (totDelta && sumWritten) {
    const wd = sumWritten - target;
    totDelta.textContent = (wd > 0 ? '+' : '') + wd.toLocaleString('ru-RU');
    totDelta.className   = `plan-delta-cell ${Math.abs(wd) <= 50 ? 'ok' : wd > 0 ? 'over' : 'under'}`;
  }
}

function _updatePlanTotalsFromInputs() {
  // Read all inputs to compute live sum without saving
  const struct = getStructure();
  const blocks = struct.blocks || [];
  const target = planTarget();
  let sum = 0;
  blocks.forEach((_, i) => {
    const inp = $(`plan-input-${i}`);
    sum += inp ? (parseInt(inp.value) || 0) : 0;
  });
  const diff  = sum - target;
  const sumEl = $('plan-sum');
  if (sumEl) {
    sumEl.textContent = `${sum.toLocaleString('ru-RU')} / ${target.toLocaleString('ru-RU')}`;
    sumEl.className   = `plan-sum ${Math.abs(diff) <= 10 ? 'ok' : diff > 0 ? 'over' : 'under'}`;
  }
  const totTarget = $('plan-total-target');
  if (totTarget) totTarget.textContent = sum.toLocaleString('ru-RU');
}

function updatePlanRow(i, writtenLen) {
  // Called after each block is written to update the row
  const writtenEl = $(`plan-written-${i}`);
  const deltaEl   = $(`plan-delta-${i}`);
  const rowEl     = $(`plan-row-${i}`);
  const struct    = getStructure();
  const block     = struct.blocks?.[i];
  if (!block) return;

  const target = block.target_chars || 0;
  const delta  = writtenLen - target;
  if (writtenEl) writtenEl.textContent = writtenLen.toLocaleString('ru-RU');
  if (deltaEl) {
    deltaEl.innerHTML = `<span class="plan-delta-cell ${Math.abs(delta) <= 50 ? 'ok' : delta > 0 ? 'over' : 'under'}">${delta > 0 ? '+' : ''}${delta}</span>`;
  }
  if (rowEl) rowEl.className = 'plan-row-done';

  // Update totals
  const writtenArr = P.stages.block_writer.blocks || [];
  _updatePlanTotals(struct.blocks || [], writtenArr);
}

// ─── Block card helpers ───────────────────────────────────

function renderBlockCards(texts) {
  const container = $('blocks-container');
  const structure = getStructureBlocks();
  container.innerHTML = '';

  texts.forEach((text, i) => {
    const name = structure[i]?.block_name || `Блок ${i + 1}`;
    const div  = createBlockCard(i + 1, texts.length, name, text || '', text ? 'done' : 'pending');
    container.appendChild(div);
  });

  const combined = texts.filter(Boolean).join('\n\n---\n\n');
  $('result-block_writer').value = combined;
  updateCount('block_writer');
  updateBlocksLabel(texts.filter(Boolean).length, texts.length);
}

function createBlockCard(num, total, name, text, state) {
  const div = document.createElement('div');
  div.className = `block-item ${state}`;
  div.id = `block-card-${num - 1}`;
  const chars = text ? text.length.toLocaleString('ru-RU') + ' симв' : '';
  div.innerHTML = `
    <div class="block-item-header" onclick="toggleBlockItem(${num - 1})">
      <span class="block-item-num">${num}/${total}</span>
      <span class="block-item-name">${escHtml(name)}</span>
      <span class="block-item-chars" id="block-chars-${num - 1}">${chars}</span>
      <span class="block-item-toggle">▼</span>
    </div>
    <div class="block-item-content" id="block-content-${num - 1}"></div>`;
  if (text && state === 'done') {
    // replace preview div with full-height textarea immediately
    _setBlockDone(div, num - 1, text);
    // Done blocks start collapsed
    div.classList.add('block-collapsed');
  }
  return div;
}

function toggleBlockItem(i) {
  const card = $(`block-card-${i}`);
  if (!card) return;
  if (card.classList.contains('active')) return;
  const opening = card.classList.contains('block-collapsed');
  card.classList.toggle('block-collapsed');
  if (opening) _refitBlockTextarea(card);
}

function expandAllBlocks() {
  document.querySelectorAll('.block-item:not(.active)').forEach(c => {
    c.classList.remove('block-collapsed');
    _refitBlockTextarea(c);
  });
}

function collapseAllBlocks() {
  document.querySelectorAll('.block-item:not(.active)').forEach(c => {
    c.classList.add('block-collapsed');
  });
}

function _refitBlockTextarea(card) {
  // height fixed via CSS (500px), nothing to recalculate
}

function _setBlockDone(card, i, text) {
  // Remove streaming preview div, insert auto-height textarea
  const old = card.querySelector('.block-item-content');
  if (old) old.remove();

  const ta = document.createElement('textarea');
  ta.className = 'block-item-textarea';
  ta.id = `block-content-${i}`;
  ta.readOnly = true;
  ta.value = text;
  card.appendChild(ta);
  // Height set after paint so scrollHeight is correct
  requestAnimationFrame(() => autoHeight(ta));
}

function autoHeight(ta) {
  ta.style.height = 'auto';
  ta.style.height = ta.scrollHeight + 'px';
}

function updateBlockCard(i, text, state) {
  const card  = $(`block-card-${i}`);
  const chars = $(`block-chars-${i}`);
  if (!card) return;

  // Preserve collapsed state before changing class
  const wasCollapsed = card.classList.contains('block-collapsed');
  card.className = `block-item ${state}`;
  if (chars) chars.textContent = text ? text.length.toLocaleString('ru-RU') + ' симв' : '';

  if (state === 'active') {
    // Streaming: always open, use preview div with blinking cursor
    card.classList.remove('block-collapsed');
    let content = $(`block-content-${i}`);
    if (!content || content.tagName === 'TEXTAREA') {
      const div = document.createElement('div');
      div.className = 'block-item-content';
      div.id = `block-content-${i}`;
      if (content) content.replaceWith(div);
      else card.appendChild(div);
      content = div;
    }
    content.innerHTML = escHtml(text) + '<span class="block-cursor"></span>';
    content.scrollTop = content.scrollHeight;

  } else if (state === 'done') {
    // Final: swap to auto-height readonly textarea, then collapse
    const existing = $(`block-content-${i}`);
    if (existing && existing.tagName === 'TEXTAREA') {
      existing.value = text;
      autoHeight(existing);
    } else {
      _setBlockDone(card, i, text);
    }
    // Collapse when done (unless user had it open)
    if (!wasCollapsed || state === 'done') {
      card.classList.add('block-collapsed');
    }
  }
}

function updateBlocksLabel(done, total) {
  const el = $('blocks-count-label');
  if (el) el.textContent = total ? `${done} / ${total}` : '';
}

function escHtml(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function setBadge(stage, state) {
  const badge = $(`badge-${stage}`);
  const card  = $(`card-${stage}`);
  if (!badge || !card) return;

  badge.className = `stage-badge ${state}`;
  card.className  = `stage-card ${state}`;

  const labels = { idle: 'idle', running: 'running...', done: 'done', error: 'error' };
  badge.textContent = labels[state] || state;
}

// ─── Card collapse ────────────────────────────────────────
function toggleCard(stage) {
  collapsed[stage] = !collapsed[stage];
  const body  = $(`body-${stage}`);
  const arrow = $(`arrow-${stage}`);
  if (collapsed[stage]) {
    body.classList.add('collapsed');
    arrow.classList.remove('open');
  } else {
    body.classList.remove('collapsed');
    arrow.classList.add('open');
  }
}

// ─── Prompt lock ──────────────────────────────────────────
function toggleLock(stage) {
  locks[stage] = !locks[stage];
  const ta   = $(`prompt-${stage}`);
  const btn  = $(`lock-${stage}`);
  ta.readOnly = locks[stage];
  btn.textContent = locks[stage] ? '🔒' : '🔓';
}

function onPromptChange(stage, value) {
  if (!P.stages[stage]) return;
  P.stages[stage].prompt = value;
  scheduleSave();
}

// ─── Copy ─────────────────────────────────────────────────
function copyResult(stage) {
  const text = $(`result-${stage}`).value;
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => log(`Скопировано: ${stage}`, 'info'));
}

// ─── Log ──────────────────────────────────────────────────
function log(msg, type = '') {
  const body = $('log-body');
  if (!body) return;
  if (isRunning) ensureRuntimeVisibility();
  const now = new Date();
  const time = now.toTimeString().slice(0, 8);
  const entry = document.createElement('div');
  entry.className = 'log-entry';
  entry.innerHTML = `<span class="log-time">${time}</span><span class="log-msg ${type}">${msg}</span>`;
  body.appendChild(entry);
  body.scrollTop = body.scrollHeight;
}

function clearLog() {
  const b = $('log-body');
  if (b) b.innerHTML = '';
}

// ─── Save ─────────────────────────────────────────────────
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveProject, 800);
}

async function saveProject() {
  try {
    await fetch(`/project/${PID}/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name:                   P.name,
        source_text:            P.source_text,
        master_prompt:          P.master_prompt,
        hero_prompt:            P.hero_prompt,
        duration_minutes:       P.duration_minutes,
        chars_per_minute:       P.chars_per_minute,
        humanize_mode:          P.humanize_mode || 'norm',
        voice_language:         P.voice_language || 'ru',
        scene_duration_seconds: P.scene_duration_seconds || 6,
        stages:                 P.stages,
      }),
    });
  } catch (e) {
    log('Ошибка автосохранения: ' + e.message, 'error');
  }
}

// ─── Streaming helper ─────────────────────────────────────
async function streamRequest(url, body, { onStatus, onDelta, onReplace, onResult, onError } = {}) {
  const signal = body.__signal;
  const payload = { ...body };
  delete payload.__signal;
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });

  if (!resp.ok) {
    onError?.(`HTTP ${resp.status}`);
    return;
  }

  const reader  = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop() || '';
    for (const line of lines) {
      const t = line.trim();
      if (!t) continue;
      try {
        const d = JSON.parse(t);
        if (d.type === 'status')  onStatus?.(d.message);
        if (d.type === 'delta')   onDelta?.(d.content);
        if (d.type === 'replace') onReplace?.(d.content);
        if (d.type === 'result')  onResult?.(d.content);
        if (d.type === 'error')   onError?.(d.message);
      } catch (_) { /* partial line */ }
    }
  }
}

function setStageStopVisible(stage, visible) {
  const btn = $(`btn-stop-${stage}`);
  if (!btn) return;
  btn.style.display = visible ? 'inline-flex' : 'none';
}

function stopStage(stage) {
  if (activeStage !== stage) return;
  stopRequested = true;
  if (activeAbortController) activeAbortController.abort();
  log(`⏹ Остановлено: ${stage}`, 'warning');
}

// ─── Single stage runner ──────────────────────────────────
async function runStage(stage) {
  if (isRunning) { log('Конвейер уже запущен', 'warning'); return; }
  isRunning = true;
  _didAutoScroll = false;
  setRunAllBtn(true);

  const resultEl = $(`result-${stage}`);
  resultEl.value = '';
  resultEl.classList.add('streaming');
  setBadge(stage, 'running');
  setStageStopVisible(stage, true);
  activeStage = stage;
  stopRequested = false;
  log(`▶ Запускаем: ${stage}`, 'info');

  // Open card if collapsed
  if (collapsed[stage]) toggleCard(stage);

  let success = true;
  try {
    const runPayload = { stage };
    if (stage === 'humanize_tts') {
      const mode = $('humanize-mode')?.value || 'norm';
      P.humanize_mode = mode;
      runPayload.humanize_mode = mode;
      log(`⚙ Humanize mode: ${mode}`, 'info');
    }

    activeAbortController = new AbortController();
    await streamRequest(
      `/project/${PID}/run`,
      { ...runPayload, __signal: activeAbortController.signal },
      {
        onStatus: msg => log(msg, ''),
        onReplace: full => {
          resultEl.value = full;
          scrollResultToBottom(resultEl);
          P.stages[stage === 'block_writer' ? 'block_writer' : stage].result = full;
          updateCount(stage);
        },
        onDelta: chunk => {
          resultEl.value += chunk;
          scrollResultToBottom(resultEl);
          P.stages[stage === 'block_writer' ? 'block_writer' : stage].result = resultEl.value;
        },
        onResult: full => {
          resultEl.value = full;
          scrollResultToBottom(resultEl);
          P.stages[stage === 'block_writer' ? 'block_writer' : stage].result = full;
          updateCount(stage);
          setBadge(stage, 'done');
          log(`✓ ${stage} завершён — ${full.length.toLocaleString('ru-RU')} символов`, 'success');
          if (stage === 'final') updateSceneBtnState();
          scheduleSave();
        },
        onError: msg => {
          setBadge(stage, 'error');
          log(`✗ ${stage}: ${msg}`, 'error');
          success = false;
        },
      }
    );
  } catch (e) {
    if (e.name === 'AbortError') {
      setBadge(stage, 'error');
      log(`⏹ ${stage}: остановлено`, 'warning');
      success = false;
    } else {
      setBadge(stage, 'error');
      log(`✗ Ошибка: ${e.message}`, 'error');
      success = false;
    }
  } finally {
    resultEl.classList.remove('streaming');
    setStageStopVisible(stage, false);
    if (activeStage === stage) activeStage = null;
    activeAbortController = null;
    isRunning = false;
    setRunAllBtn(false);
  }
  return success;
}

// ─── Block Writer loop ────────────────────────────────────
async function runBlockWriter() {
  ensureRuntimeVisibility();
  if (!P.stages.structure.result) { log('✗ Сначала выполните Structure', 'error'); return false; }

  // Use getStructure() which handles both array and object formats
  const structure = getStructure();
  const blocks = structure.blocks || [];
  if (!blocks.length) { log('✗ В Structure нет блоков', 'error'); return false; }

  // Analysis returns a raw array of segments (0-indexed)
  let analysisSegs = [];
  try {
    const raw = JSON.parse(P.stages.analysis.result || '[]');
    analysisSegs = Array.isArray(raw) ? raw : (raw.segments || []);
  } catch (_) {}

  setBadge('block_writer', 'running');
  setStageStopVisible('block_writer', true);
  activeStage = 'block_writer';
  stopRequested = false;
  if (collapsed['block_writer']) toggleCard('block_writer');

  // Progress bar
  const progressWrap  = $('block-progress-wrap');
  const progressFill  = $('progress-fill');
  const progressLabel = $('block-progress-label');
  progressWrap.style.display = 'flex';

  // Init cards
  const container = $('blocks-container');
  container.innerHTML = '';
  const allBlocks = new Array(blocks.length).fill('');

  blocks.forEach((b, i) => {
    const card = createBlockCard(i + 1, blocks.length, b.block_name || b.title || `Блок ${i + 1}`, '', 'pending');
    container.appendChild(card);
  });

  updateBlocksLabel(0, blocks.length);
  let lastBlockTail = '';

  for (let i = 0; i < blocks.length; i++) {
    if (stopRequested) {
      log('⏹ Остановлено пользователем', 'warning');
      setBadge('block_writer', 'error');
      progressWrap.style.display = 'none';
      return false;
    }

    const block = blocks[i];
    // source_segment_ids are 0-based indices into the analysis array
    // If empty — fall back to a window of segments proportional to block position
    let segsArr = (block.source_segment_ids || [])
      .map(id => analysisSegs[id])
      .filter(Boolean);
    if (!segsArr.length && analysisSegs.length) {
      // Fallback: distribute segments evenly across blocks
      const segsPerBlock = Math.ceil(analysisSegs.length / blocks.length);
      const start = Math.min(i * segsPerBlock, analysisSegs.length - 1);
      const end   = Math.min(start + segsPerBlock, analysisSegs.length);
      segsArr = analysisSegs.slice(start, end);
    }
    const sourceSegmentsText = segsArr.map((s, idx) => {
      const facts = Array.isArray(s.key_facts) ? s.key_facts.join(', ') : (s.key_facts || '');
      return `[seg_${idx + 1} importance:${s.importance || 'B'}]\n${s.segment}\nКак усилить: ${s.emotion_boost || ''}\nФакты: ${facts}`;
    }).join('\n\n');

    const blockLabel = block.block_name || block.title || `Блок ${i + 1}`;
    progressLabel.textContent = `Блок ${i + 1} из ${blocks.length}: «${blockLabel}»`;
    progressFill.style.width  = `${(i / blocks.length) * 100}%`;
    log(`📝 Генерирую блок ${i + 1}/${blocks.length}: «${blockLabel}»`);

    // Scroll card into view
    const card = $(`block-card-${i}`);
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    updateBlockCard(i, '', 'active');

    let blockText = '';
    let blockOk   = true;

    try {
      activeAbortController = new AbortController();
      await streamRequest(
        `/project/${PID}/run`,
        {
          stage:           'block_writer',
          block_index:     i,
          block_data:      block,
          source_segments: sourceSegmentsText,
          last_block_tail: lastBlockTail,
          total_blocks:    blocks.length,
          __signal:        activeAbortController.signal,
        },
        {
          onStatus: msg => log(msg),
          onDelta: chunk => {
            blockText += chunk;
            updateBlockCard(i, blockText, 'active');
            allBlocks[i] = blockText;
            $('result-block_writer').value = allBlocks.join('\n\n---\n\n');
            updateCount('block_writer');
          },
          onReplace: corrected => {
            // Backend ran a correction pass — replace streamed content
            blockText = corrected;
            updateBlockCard(i, corrected, 'active');
            allBlocks[i] = corrected;
            $('result-block_writer').value = allBlocks.join('\n\n---\n\n');
            updateCount('block_writer');
          },
          onResult: full => {
            allBlocks[i] = full;
            updateBlockCard(i, full, 'done');
            P.stages.block_writer.blocks = [...allBlocks];
            updateBlocksLabel(i + 1, blocks.length);
            updatePlanRow(i, full.length);          // ← update plan table row
            lastBlockTail = (full || '').slice(-150);
          },
          onError: msg => {
            log(`✗ Блок ${i + 1}: ${msg}`, 'error');
            updateBlockCard(i, blockText || '(ошибка)', 'error');
            blockOk = false;
          },
        }
      );
    } catch (e) {
      if (e.name === 'AbortError') {
        stopRequested = true;
        blockOk = false;
      } else {
        log(`✗ Блок ${i + 1}: ${e.message}`, 'error');
        blockOk = false;
      }
    } finally {
      activeAbortController = null;
    }

    if (!blockOk || stopRequested) {
      setBadge('block_writer', 'error');
      progressWrap.style.display = 'none';
      setStageStopVisible('block_writer', false);
      activeAbortController = null;
      if (activeStage === 'block_writer') activeStage = null;
      return false;
    }

    log(`✓ Блок ${i + 1}/${blocks.length}: ${allBlocks[i].length.toLocaleString('ru-RU')} симв`, 'success');
  }

  progressFill.style.width = '100%';
  progressLabel.textContent = `Все ${blocks.length} блоков готовы ✓`;
  setTimeout(() => { progressWrap.style.display = 'none'; }, 2500);

  const combined = allBlocks.join('\n\n---\n\n');
  $('result-block_writer').value = combined;
  P.stages.block_writer.result   = combined;
  P.stages.block_writer.blocks   = allBlocks;

  updateCount('block_writer');
  setBadge('block_writer', 'done');
  setStageStopVisible('block_writer', false);
  activeAbortController = null;
  if (activeStage === 'block_writer') activeStage = null;
  log(`✓ Block Writer: ${blocks.length} блоков, ${combined.length.toLocaleString('ru-RU')} символов`, 'success');
  await saveProject();
  return true;
}

// ─── Run All ──────────────────────────────────────────────
async function runAll() {
  if (isRunning) return;
  isRunning = true;
  _didAutoScroll = false;
  stopRequested = false;
  setRunAllBtn(true);
  $('btn-stop').style.display = 'inline-flex';
  log('🚀 Запускаем весь конвейер...', 'info');

  const runSimple = async (stage) => {
    const resultEl = $(`result-${stage}`);
    resultEl.value = '';
    resultEl.classList.add('streaming');
    setBadge(stage, 'running');
    if (collapsed[stage]) toggleCard(stage);
    log(`▶ ${stage}...`, 'info');

    let ok = true;
    await streamRequest(
      `/project/${PID}/run`,
      { stage },
      {
        onStatus: msg => log(msg),
        onReplace: full => {
          resultEl.value = full;
          scrollResultToBottom(resultEl);
          P.stages[stage].result = full;
          updateCount(stage);
        },
        onDelta: chunk => {
          resultEl.value += chunk;
          scrollResultToBottom(resultEl);
          P.stages[stage].result = resultEl.value;
        },
        onResult: full => {
          resultEl.value = full;
          scrollResultToBottom(resultEl);
          P.stages[stage].result = full;
          updateCount(stage);
          setBadge(stage, 'done');
          log(`✓ ${stage}: ${full.length.toLocaleString('ru-RU')} символов`, 'success');
          // After structure — render the block plan editor
          if (stage === 'structure') renderBlockPlan();
          scheduleSave();
        },
        onError: msg => {
          setBadge(stage, 'error');
          log(`✗ ${stage}: ${msg}`, 'error');
          ok = false;
        },
      }
    );
    resultEl.classList.remove('streaming');
    return ok;
  };

  try {
    if (!await runSimple('pre_analysis'))   { done(); return; }
    if (stopRequested)                      { done(); return; }
    if (!await runSimple('analysis'))       { done(); return; }
    if (stopRequested)                      { done(); return; }
    if (!await runSimple('structure'))      { done(); return; }
    if (stopRequested)                      { done(); return; }
    isRunning = false;   // release for blockWriter's own guard
    if (!await runBlockWriter())         { done(); return; }
    isRunning = true;
    if (stopRequested)                      { done(); return; }
    if (!await runSimple('merger'))         { done(); return; }
    if (stopRequested)                      { done(); return; }
    if (!await runSimple('quality_check'))  { done(); return; }
    if (stopRequested)                      { done(); return; }
    if (!await runSimple('final'))          { done(); return; }
    log('🎉 Конвейер завершён!', 'success');
  } catch (e) {
    log(`✗ Необработанная ошибка: ${e.message}`, 'error');
  }

  done();
  function done() {
    isRunning = false;
    stopRequested = false;
    setRunAllBtn(false);
    $('btn-stop').style.display = 'none';
  }
}

function setRunAllBtn(running) {
  const btn = $('btn-run-all');
  btn.disabled = running;
  btn.textContent = running ? '⏳ Выполняется...' : '▶▶ Запустить всё';
}

// ─── Event Listeners ──────────────────────────────────────
function setupEventListeners() {
  // Source text
  $('source-text').addEventListener('input', e => {
    P.source_text = e.target.value;
    updateSourceCount();
    scheduleSave();
  });

  // Master prompt
  $('master-prompt').addEventListener('input', e => {
    P.master_prompt = e.target.value;
    scheduleSave();
  });

  // Master prompt lock
  $('lock-master').addEventListener('click', () => {
    locks.master = !locks.master;
    $('master-prompt').readOnly = locks.master;
    $('lock-master').textContent = locks.master ? '🔒' : '🔓';
  });

  // Hero prompt
  $('hero-prompt').addEventListener('input', e => {
    P.hero_prompt = e.target.value;
    scheduleSave();
  });

  // Duration slider
  $('duration-slider').addEventListener('input', e => {
    P.duration_minutes = parseInt(e.target.value);
    updateDurationDisplay();
    scheduleSave();
  });

  // Chars per minute
  $('chars-per-min').addEventListener('input', e => {
    P.chars_per_minute = parseInt(e.target.value) || 700;
    updateDurationDisplay();
    scheduleSave();
  });

  // Humanize mode
  $('humanize-mode').addEventListener('change', e => {
    P.humanize_mode = e.target.value || 'norm';
    scheduleSave();
  });

  // Voice language
  $('voice-language-select').addEventListener('change', e => {
    P.voice_language = e.target.value || 'ru';
    // Update scene duration hint to reflect new language CPM
    const sds = parseInt($('scene-duration-slider').value) || 6;
    updateSceneDurationDisplay(sds);
    scheduleSave();
  });

  // Scene duration slider
  $('scene-duration-slider').addEventListener('input', e => {
    onSceneDurationChange(e.target.value);
  });

  // Run all
  $('btn-run-all').addEventListener('click', runAll);

  // Stop
  $('btn-stop').addEventListener('click', () => {
    stopRequested = true;
    log('⏹ Запрошена остановка...', 'warning');
  });

  // Delete project
  $('btn-delete').addEventListener('click', async () => {
    if (!confirm(`Удалить проект «${P.name}»?`)) return;
    await fetch(`/project/${PID}/delete`, { method: 'POST' });
    window.location.href = '/';
  });

  // Rename (click on name display)
  const nameDisplay = $('project-name-display');
  const nameInput   = $('project-name-input');

  nameDisplay.addEventListener('click', () => {
    nameInput.value = P.name;
    nameDisplay.style.display = 'none';
    nameInput.style.display   = 'block';
    nameInput.focus();
    nameInput.select();
  });

  const commitRename = async () => {
    const newName = nameInput.value.trim() || P.name;
    P.name = newName;
    setTitle(newName);
    nameInput.style.display   = 'none';
    nameDisplay.style.display = 'block';
    await fetch(`/project/${PID}/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: newName }),
    });
  };

  nameInput.addEventListener('blur', commitRename);
  nameInput.addEventListener('keydown', e => { if (e.key === 'Enter') nameInput.blur(); });

  // Block writer stage button maps to loop
  $('btn-block_writer').addEventListener('click', async (e) => {
    e.stopPropagation();
    if (isRunning) return;
    isRunning = true;
    setRunAllBtn(true);
    await runBlockWriter();
    isRunning = false;
    setRunAllBtn(false);
  });
}

// ─── Scene Builder ────────────────────────────────────────

function onSceneDurationChange(val) {
  const secs = parseInt(val);
  P.scene_duration_seconds = secs;
  updateSceneDurationDisplay(secs);
  scheduleSave();
}

function onSceneStyleChange(val) {
  P.scene_style_prefix = val;
  scheduleSave();
}

function updateSceneDurationDisplay(secs) {
  const CPM      = { ru: 850, en: 700 };
  const lang     = P.voice_language || 'ru';
  const cpm      = CPM[lang] || 850;
  const chars    = Math.round(secs * cpm / 60);
  const charsAlt = Math.round(secs * (lang === 'ru' ? 700 : 850) / 60);
  const altLang  = lang === 'ru' ? 'en' : 'ru';

  const valEl   = $('scene-duration-val');
  const hint    = $('scene-chars-hint');
  const langHnt = $('scene-lang-hint');
  if (valEl)   valEl.textContent  = `${secs} сек / сцена`;
  if (hint)    hint.textContent   = `≈ ${chars} симв (${lang})`;
  if (langHnt) langHnt.textContent = `/ ≈ ${charsAlt} (${altLang})`;
}

function updateSceneBtnState() {
  const btn = $('btn-scene_builder');
  if (!btn) return;
  const hasResult = !!(P.stages.final?.result || '').trim();
  btn.disabled = !hasResult;
  btn.title    = hasResult ? '' : 'Сначала выполните этап Final';
}

function updateSceneCount(ndjson) {
  const el = $('scene-count-label');
  if (!el) return;
  const lines = (ndjson || '').split('\n');
  let totalScenes = 0;
  let withVideo   = 0;
  let durSum      = 0;
  let durCount    = 0;
  lines.forEach(line => {
    line = line.trim();
    if (!line) return;
    try {
      const obj = JSON.parse(line);
      if (obj.scene_id)                              totalScenes++;
      if (obj.video && obj.video.prompt !== null)    withVideo++;
      if (typeof obj.duration_seconds === 'number') { durSum += obj.duration_seconds; durCount++; }
    } catch (_) {}
  });
  if (!totalScenes) { el.textContent = ''; return; }
  const pct    = Math.round(withVideo / totalScenes * 100);
  const avgDur = durCount ? (durSum / durCount).toFixed(1) : null;
  const durPart = avgDur ? ` · ср. ${avgDur}с` : '';
  el.textContent = `· ${totalScenes} сцен · ${withVideo} с видео (${pct}%)${durPart}`;
}

async function resetSceneBuilderPrompt() {
  try {
    const res  = await fetch('/prompt/scene_builder');
    const data = await res.json();
    const fresh = data.prompt || '';
    $('prompt-scene_builder').value = fresh;
    P.stages.scene_builder.prompt   = fresh;
    scheduleSave();
    log('↺ Промпт Scene Builder сброшен до актуальной версии', 'info');
  } catch (e) {
    log('✗ Не удалось загрузить промпт: ' + e.message, 'error');
  }
}

async function runSceneBuilder() {
  if (isRunning) { log('Конвейер уже запущен', 'warning'); return; }

  const finalResult = (P.stages.final?.result || '').trim();
  if (!finalResult) {
    log('✗ Scene Builder: результат Final пустой', 'error');
    return;
  }

  isRunning = true;
  setRunAllBtn(true);
  _didAutoScroll = false;

  const secs       = parseInt($('scene-duration-slider').value) || 6;
  const cpm        = parseInt(P.chars_per_minute) || 700;
  const charsScene = Math.round(secs * cpm / 60);

  const resultEl = $('result-scene_builder');
  resultEl.value = '';
  resultEl.classList.add('streaming');
  setBadge('scene_builder', 'running');
  setStageStopVisible('scene_builder', true);
  activeStage = 'scene_builder';
  stopRequested = false;
  if (collapsed['scene_builder']) toggleCard('scene_builder');
  $('btn-export-scenes').style.display = 'none';
  $('btn-export-scenes-text').style.display = 'none';
  const voiceLang = P.voice_language || 'ru';
  log(`▶ Scene Builder: ${secs} сек/сцена, ≈${charsScene} симв/сцена (${voiceLang})`, 'info');

  try {
    activeAbortController = new AbortController();
    await streamRequest(
      `/project/${PID}/run`,
      {
        stage: 'scene_builder',
        scene_duration_seconds: secs,
        chars_per_scene: charsScene,
        voice_language: voiceLang,
        __signal: activeAbortController.signal,
      },
      {
        onStatus: msg => log(msg, ''),
        onDelta: chunk => {
          resultEl.value += chunk;
          scrollResultToBottom(resultEl);
          P.stages.scene_builder.result = resultEl.value;
        },
        onReplace: full => {
          resultEl.value = full;
          scrollResultToBottom(resultEl);
          P.stages.scene_builder.result = full;
          updateCount('scene_builder');
        },
        onResult: full => {
          resultEl.value = full;
          resultEl.scrollTop = 0;          // show scene_001 first
          P.stages.scene_builder.result = full;
          updateCount('scene_builder');
          updateSceneCount(full);
          setBadge('scene_builder', 'done');
          $('btn-export-scenes').style.display = 'inline-flex';
          $('btn-export-scenes-text').style.display = 'inline-flex';
          let sceneCount = 0, videoCount = 0;
          full.split('\n').forEach(l => {
            l = l.trim(); if (!l) return;
            try {
              const o = JSON.parse(l);
              if (o.scene_id) sceneCount++;
              if (o.video && o.video.prompt !== null) videoCount++;
            } catch (_) {}
          });
          const pct = sceneCount ? Math.round(videoCount / sceneCount * 100) : 0;
          log(`✓ Scene Builder: ${sceneCount} сцен · ${videoCount} с видео (${pct}%)`, 'success');
          scheduleSave();
        },
        onError: msg => {
          setBadge('scene_builder', 'error');
          log(`✗ Scene Builder: ${msg}`, 'error');
        },
      }
    );
  } catch (e) {
    if (e.name === 'AbortError') {
      setBadge('scene_builder', 'error');
      log('⏹ Scene Builder: остановлено', 'warning');
    } else {
      setBadge('scene_builder', 'error');
      log(`✗ Ошибка: ${e.message}`, 'error');
    }
  } finally {
    resultEl.classList.remove('streaming');
    setStageStopVisible('scene_builder', false);
    if (activeStage === 'scene_builder') activeStage = null;
    activeAbortController = null;
    isRunning = false;
    setRunAllBtn(false);
  }
}
