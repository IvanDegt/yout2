/* ═══════════════════════════════════════════════════════════
   ReWrite Master — Transcription JS
═══════════════════════════════════════════════════════════ */

// ─── State ────────────────────────────────────────────────
let isRunning        = false;
let isTranslating    = false;
let currentText      = { original: '', translation: '' };
let pendingText      = '';   // which text to use in "→ В проект"
let translateOutput  = '';   // text from standalone translator

// ─── Init ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  setupUrlInput();
  setupTranslateInput();
  $('btn-start').addEventListener('click', startTranscription);
  $('btn-paste').addEventListener('click', pasteUrl);
  $('btn-clear-url').addEventListener('click', clearUrl);
  $('btn-new-project').addEventListener('click', createProjectWithText);
  $('btn-translate').addEventListener('click', startTranslation);

  // Keyboard shortcut: Enter in URL input
  $('url-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') startTranscription();
  });
});

function $(id) { return document.getElementById(id); }

// ─── URL input helpers ────────────────────────────────────
function setupUrlInput() {
  const input   = $('url-input');
  const clearBtn = $('btn-clear-url');

  input.addEventListener('input', () => {
    clearBtn.style.display = input.value ? 'flex' : 'none';
    // Hide video info if URL changes
    $('video-info').style.display = 'none';
  });
}

async function pasteUrl() {
  try {
    const text = await navigator.clipboard.readText();
    $('url-input').value = text.trim();
    $('url-input').dispatchEvent(new Event('input'));
    $('url-input').focus();
  } catch (e) {
    $('url-input').focus();
  }
}

function clearUrl() {
  $('url-input').value = '';
  $('url-input').dispatchEvent(new Event('input'));
  $('video-info').style.display = 'none';
  $('url-input').focus();
}

// ─── Steps UI ─────────────────────────────────────────────
const STEP_COUNT = 4;

function setStep(n, state, msg) {
  const el  = $(`step-${n}`);
  const msgEl = $(`step-${n}-msg`);
  if (!el) return;
  el.className = `tr-step ${state}`;
  if (msgEl && msg !== undefined) msgEl.textContent = msg;
}

function setAllStepsPending() {
  for (let i = 1; i <= STEP_COUNT; i++) setStep(i, 'pending', '');
}

function showSteps() {
  $('tr-steps').style.display = 'flex';
}

function updateDownloadProgress(pct, msg) {
  const prog = $('step-2-progress');
  const fill = $('step-2-fill');
  const pctEl = $('step-2-pct');
  const msgEl = $('step-2-msg');
  if (prog) prog.style.display = 'flex';
  if (fill) fill.style.width   = `${Math.min(pct, 100)}%`;
  if (pctEl) pctEl.textContent = `${Math.round(pct)}%`;
  if (msgEl && msg) msgEl.textContent = msg;
}

// ─── Error ────────────────────────────────────────────────
function showError(msg) {
  $('error-msg').textContent = msg;
  $('error-banner').style.display = 'flex';
}

function dismissError() {
  $('error-banner').style.display = 'none';
}

// ─── Results UI ───────────────────────────────────────────
function showResults() {
  $('tr-results').style.display = 'flex';
}

function setOriginal(text, langName, flag) {
  $('original-textarea').value = text;
  $('original-lang-label').textContent = `Исходник (${langName})`;
  if (flag) $('original-flag').textContent = flag;
  updateStats('original-stats', text);
  currentText.original = text;
  autoHeight($('original-textarea'));
}

function appendTranslationDelta(chunk) {
  const ta = $('translation-textarea');
  ta.value += chunk;
  ta.scrollTop = ta.scrollHeight;
  currentText.translation = ta.value;
  ta.classList.add('streaming');
}

function finalizeTranslation(text) {
  const ta = $('translation-textarea');
  ta.value = text;
  ta.classList.remove('streaming');
  updateStats('translation-stats', text);
  currentText.translation = text;
  autoHeight(ta);
}

function updateStats(elId, text) {
  const el = $(elId);
  if (!el) return;
  const chars = text.length;
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  el.textContent = `${words.toLocaleString('ru-RU')} слов • ${chars.toLocaleString('ru-RU')} симв`;
}

function autoHeight(ta) {
  if (!ta) return;
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 480) + 'px';
}

function langFlag(code) {
  const flags = {
    en: '🇬🇧', ru: '🇷🇺', de: '🇩🇪', fr: '🇫🇷', es: '🇪🇸',
    it: '🇮🇹', pt: '🇵🇹', zh: '🇨🇳', ja: '🇯🇵', ko: '🇰🇷',
    ar: '🇸🇦', uk: '🇺🇦', pl: '🇵🇱', tr: '🇹🇷', nl: '🇳🇱',
  };
  return flags[code] || '🌐';
}

// ─── Copy ─────────────────────────────────────────────────
function copyText(taId) {
  const ta = $(taId);
  if (!ta || !ta.value) return;
  navigator.clipboard.writeText(ta.value).then(() => {
    showToast('Скопировано!');
  });
}

function showToast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.classList.add('toast-visible'), 10);
  setTimeout(() => {
    t.classList.remove('toast-visible');
    setTimeout(() => t.remove(), 300);
  }, 1800);
}

// ─── Use as source ────────────────────────────────────────
function useAsSource(which) {
  pendingText = currentText[which] || '';
  if (!pendingText) return;
  $('use-modal').style.display = 'flex';
}

function closeUseModal() {
  $('use-modal').style.display = 'none';
  pendingText = '';
}

async function createProjectWithText() {
  if (!pendingText) return;
  try {
    const res  = await fetch('/project/new', { method: 'POST' });
    const data = await res.json();
    // Save source text to the new project
    await fetch(`/project/${data.id}/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_text: pendingText }),
    });
    window.location.href = `/project/${data.id}`;
  } catch (e) {
    showError('Не удалось создать проект: ' + e.message);
    closeUseModal();
  }
}

// ─── Translate: input stats ───────────────────────────────
function setupTranslateInput() {
  const ta = $('tr-input-text');
  if (!ta) return;
  ta.addEventListener('input', () => {
    updateStats('tr-input-stats', ta.value);
  });
}

function clearTranslateInput() {
  $('tr-input-text').value = '';
  updateStats('tr-input-stats', '');
  $('tr-output-text').value = '';
  updateStats('tr-output-stats', '');
  translateOutput = '';
  $('tr-translate-error').style.display = 'none';
}

function useTranslation() {
  if (!translateOutput) return;
  pendingText = translateOutput;
  $('use-modal').style.display = 'flex';
}

// ─── Main: start translation ──────────────────────────────
async function startTranslation() {
  if (isTranslating) return;
  const text = ($('tr-input-text').value || '').trim();
  if (!text) { $('tr-input-text').focus(); return; }

  const targetLang = $('tr-target-lang').value || 'ru';
  const btn        = $('btn-translate');
  const btnText    = $('btn-translate-text');
  const icon       = $('tr-icon');
  const outTa      = $('tr-output-text');
  const errEl      = $('tr-translate-error');

  isTranslating         = true;
  btn.disabled          = true;
  icon.textContent      = '⏳';
  btnText.textContent   = 'Переводим...';
  outTa.value           = '';
  outTa.classList.add('streaming');
  errEl.style.display   = 'none';
  translateOutput       = '';

  // Update output label
  const langLabels = {
    ru: '🇷🇺 Русский', en: '🇬🇧 English', de: '🇩🇪 Deutsch',
    fr: '🇫🇷 Français', es: '🇪🇸 Español', it: '🇮🇹 Italiano',
    pt: '🇵🇹 Português', zh: '🇨🇳 Китайский', ja: '🇯🇵 Японский',
    ko: '🇰🇷 Корейский', uk: '🇺🇦 Украинский',
  };
  $('tr-output-label').textContent = langLabels[targetLang] || 'Перевод';

  try {
    const resp = await fetch('/translate/run', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ text, target_lang: targetLang }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

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
          if (d.type === 'delta') {
            outTa.value    += d.content;
            translateOutput = outTa.value;
            outTa.scrollTop = outTa.scrollHeight;
          } else if (d.type === 'done') {
            translateOutput = d.text || outTa.value;
            outTa.value    = translateOutput;
            updateStats('tr-output-stats', translateOutput);
          } else if (d.type === 'error') {
            errEl.textContent    = d.message || 'Ошибка';
            errEl.style.display  = 'block';
          }
        } catch (_) {}
      }
    }
  } catch (e) {
    errEl.textContent   = e.message || 'Ошибка соединения';
    errEl.style.display = 'block';
  } finally {
    isTranslating       = false;
    btn.disabled        = false;
    icon.textContent    = '✦';
    btnText.textContent = 'Перевести';
    outTa.classList.remove('streaming');
  }
}

// ─── Main: start transcription ────────────────────────────
async function startTranscription() {
  if (isRunning) return;
  const url = $('url-input').value.trim();
  if (!url) { $('url-input').focus(); return; }

  // Reset UI
  isRunning = true;
  dismissError();
  setAllStepsPending();
  showSteps();
  $('tr-results').style.display = 'none';
  $('original-textarea').value  = '';
  $('translation-textarea').value = '';
  $('video-info').style.display = 'none';
  currentText = { original: '', translation: '' };

  const btn     = $('btn-start');
  const btnText = $('btn-start-text');
  btn.disabled  = true;
  btn.classList.add('running');
  btn.querySelector('.tr-btn-icon').textContent = '⏳';
  btnText.textContent = 'Обрабатываем...';

  let activeStep = 1;
  setStep(1, 'active', '');

  try {
    const resp = await fetch('/transcribe/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });

    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
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
          handleEvent(d);
        } catch (_) {}
      }
    }

  } catch (e) {
    showError(e.message || 'Неизвестная ошибка');
    for (let i = activeStep; i <= STEP_COUNT; i++) setStep(i, 'error', '');
  } finally {
    isRunning = false;
    btn.disabled = false;
    btn.classList.remove('running');
    btn.querySelector('.tr-btn-icon').textContent = '▶';
    btnText.textContent = 'Транскрибировать';
  }

  function handleEvent(d) {
    switch (d.type) {

      case 'step':
        // Mark previous step done, activate new one
        if (d.step > activeStep) {
          setStep(activeStep, 'done');
          activeStep = d.step;
          setStep(activeStep, 'active', d.message || '');
        } else {
          setStep(d.step, 'active', d.message || '');
        }
        break;

      case 'info':
        // Video title + duration
        $('vi-title').textContent    = d.title || '';
        $('vi-duration').textContent = d.duration
          ? `${Math.floor(d.duration / 60)}:${String(d.duration % 60).padStart(2, '0')}`
          : '';
        $('video-info').style.display = 'flex';
        setStep(1, 'done', d.message || '');
        activeStep = 2;
        setStep(2, 'active', '');
        break;

      case 'download_progress':
        updateDownloadProgress(d.pct, d.message || '');
        break;

      case 'transcript':
        setStep(3, 'done', d.message || '');
        activeStep = 4;
        showResults();
        setOriginal(d.text, d.lang_name || d.language, langFlag(d.language));
        if (d.language === 'ru') {
          // Already Russian — put in both panels
          finalizeTranslation(d.text);
          setStep(4, 'done', 'Текст уже на русском');
        } else {
          setStep(4, 'active', 'Переводим...');
          $('translation-textarea').classList.add('streaming');
        }
        break;

      case 'translation_delta':
        appendTranslationDelta(d.content);
        break;

      case 'translation_done':
        finalizeTranslation(d.text);
        setStep(4, 'done',
          `Переведено: ${(d.words || 0).toLocaleString('ru-RU')} слов`);
        break;

      case 'done':
        // All steps done
        for (let i = 1; i <= STEP_COUNT; i++) {
          const el = $(`step-${i}`);
          if (el && !el.classList.contains('done')) setStep(i, 'done');
        }
        break;

      case 'error':
        showError(d.message || 'Ошибка');
        setStep(activeStep, 'error', d.message || '');
        break;
    }
  }
}
