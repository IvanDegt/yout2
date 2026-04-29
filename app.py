import os
import json
import io
import re
import time
import tempfile
import subprocess
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_file
from dotenv import load_dotenv
import openai

load_dotenv()

from config import PROJECTS_DIR, PROMPTS_DIR, DEFAULT_MODEL, STAGE_TEMPERATURES

app = Flask(__name__)


# ─── Project helpers ───────────────────────────────────────────────────────────

def _target_total(project: dict) -> int:
    return int(project.get("duration_minutes", 0)) * int(project.get("chars_per_minute", 0))


def _normalize_structure(struct_text: str, target_total: int) -> tuple[str, str] | tuple[None, str]:
    """
    Returns (normalized_json_text, status_message) or (None, error_message).
    """
    try:
        struct = json.loads(struct_text)
        # Model sometimes returns a raw array instead of {"blocks": [...]}
        if isinstance(struct, list):
            struct = {"blocks": struct}
        struct["total_target_chars"] = target_total
        blks = struct.get("blocks", []) or []
        current_sum = sum(int(b.get("target_chars", 0) or 0) for b in blks)
        if not blks or current_sum <= 0:
            return json.dumps(struct, ensure_ascii=False, indent=2), "⚠ Структура без блоков/target_chars"

        ratio = target_total / current_sum
        for b in blks:
            b["target_chars"] = max(100, round(int(b.get("target_chars", 300) or 300) * ratio))

        # Fix rounding drift on last block
        adj_sum = sum(int(b["target_chars"]) for b in blks)
        blks[-1]["target_chars"] += (target_total - adj_sum)
        if blks[-1]["target_chars"] < 100:
            # Keep non-negative reasonable size; re-balance from earlier blocks
            deficit = 100 - blks[-1]["target_chars"]
            blks[-1]["target_chars"] = 100
            for i in range(len(blks) - 2, -1, -1):
                take = min(deficit, max(0, blks[i]["target_chars"] - 100))
                if take:
                    blks[i]["target_chars"] -= take
                    deficit -= take
                if deficit <= 0:
                    break

        # Final sanity: exact sum
        final_sum = sum(int(b["target_chars"]) for b in blks)
        if final_sum != target_total:
            blks[-1]["target_chars"] += (target_total - final_sum)

        return json.dumps(struct, ensure_ascii=False, indent=2), (
            f"✓ Структура нормализована: {len(blks)} блоков, сумма = {target_total} симв"
        )
    except Exception as e:
        return None, f"⚠ Нормализация структуры: {e}"


def _enforce_exact_length(
    client: "openai.OpenAI",
    model: str,
    *,
    system: str,
    text: str,
    target_chars: int,
    guidance: str,
    max_attempts: int = 3,
) -> tuple[str, list[str]]:
    """
    Try to rewrite text so that len(text) == target_chars exactly.
    Returns (final_text, status_messages).
    """
    msgs: list[str] = []
    cur = text or ""
    for attempt in range(1, max_attempts + 1):
        cur_len = len(cur)
        if cur_len == target_chars:
            msgs.append(f"✓ Объём точный: {cur_len} симв")
            return cur, msgs
        direction = "расширь" if cur_len < target_chars else "сократи"
        msgs.append(f"⚙ Дожим объёма (попытка {attempt}/{max_attempts}): {cur_len} → {target_chars} симв")
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": (
                    f"{guidance}\n\n"
                    f"Текущий объём: {cur_len} символов. Цель: {target_chars} символов.\n"
                    f"{direction.capitalize()} так, чтобы итоговый текст был РОВНО {target_chars} символов.\n"
                    f"Сохраняй смысл/структуру. Верни только текст.\n\n"
                    f"Текущий текст:\n{cur}"
                )},
            ],
            stream=False,
        )
        cur = (resp.choices[0].message.content or "").strip()

    # Last resort: make it exact deterministically
    cur_len = len(cur)
    if cur_len > target_chars:
        cur = cur[:target_chars]
        msgs.append(f"⚠ Жёсткое обрезание до {target_chars} симв (было {cur_len})")
    elif cur_len < target_chars:
        cur = cur + (" " * (target_chars - cur_len))
        msgs.append(f"⚠ Жёсткое добивание пробелами до {target_chars} симв (было {cur_len})")
    return cur, msgs


def _json_retry_suffix(attempt: int) -> str:
    return (
        "\n\nВАЖНО: Верни только валидный JSON. Без markdown. Без пояснений. "
        "Без тройных кавычек. Без текста вне JSON."
        f"\nПопытка: {attempt}/3"
    )


def _try_parse_json(text: str) -> tuple[dict | None, str | None]:
    try:
        return json.loads(text), None
    except Exception as e:
        return None, str(e)


def _path(project_id: str) -> str:
    return os.path.join(PROJECTS_DIR, f"{project_id}.json")


def _load(project_id: str) -> dict | None:
    p = _path(project_id)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        project = json.load(f)

    # v2 schema backfill for older projects
    stages = project.get("stages", {}) or {}
    defaults = {
        "pre_analysis":  {"prompt": _load_prompt("pre_analysis"),  "result": ""},
        "analysis":      {"prompt": _load_prompt("analysis"),      "result": ""},
        "structure":     {"prompt": _load_prompt("structure"),     "result": ""},
        "block_writer":  {"prompt": _load_prompt("block_writer"),  "result": "", "blocks": []},
        "merger":        {"prompt": _load_prompt("merger"),        "result": ""},
        "quality_check": {"prompt": _load_prompt("quality_check"), "result": ""},
        "final":         {"prompt": _load_prompt("final"),         "result": ""},
        "humanize_tts":  {"prompt": _load_prompt("humanize_tts"),  "result": ""},
        "scene_builder": {"prompt": _load_prompt("scene_builder"), "result": ""},
    }
    changed = False
    for k, v in defaults.items():
        if k not in stages:
            stages[k] = v
            changed = True
        else:
            # ensure fields exist
            for field, val in v.items():
                if field not in stages[k]:
                    stages[k][field] = val
                    changed = True
    project["stages"] = stages

    if "master_prompt" not in project:
        project["master_prompt"] = _load_prompt("master")
        changed = True
    if "hero_prompt" not in project:
        project["hero_prompt"] = ""
        changed = True
    if "duration_minutes" not in project:
        project["duration_minutes"] = 20
        changed = True
    if "chars_per_minute" not in project:
        project["chars_per_minute"] = 700
        changed = True
    if "scene_duration_seconds" not in project:
        project["scene_duration_seconds"] = 6
        changed = True
    if "voice_language" not in project:
        project["voice_language"] = "ru"
        changed = True
    if "scene_style_prefix" not in project:
        project["scene_style_prefix"] = ""
        changed = True
    if "humanize_mode" not in project:
        project["humanize_mode"] = "norm"
        changed = True

    if changed:
        _save(project)
    return project


def _save(project: dict):
    with open(_path(project["id"]), "w", encoding="utf-8") as f:
        json.dump(project, f, ensure_ascii=False, indent=2)


def _load_prompt(name: str) -> str:
    p = os.path.join(PROMPTS_DIR, f"{name}.txt")
    with open(p, "r", encoding="utf-8") as f:
        return f.read().strip()


def _list_projects() -> list:
    result = []
    if not os.path.exists(PROJECTS_DIR):
        return result
    for fname in sorted(os.listdir(PROJECTS_DIR), reverse=True):
        if not fname.endswith(".json"):
            continue
        p = _load(fname[:-5])
        if p:
            result.append(p)
    return result


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", projects=_list_projects())


@app.route("/project/new", methods=["POST"])
def new_project():
    now = datetime.now()
    pid = f"proj_{now.strftime('%Y%m%d_%H%M%S')}"
    project = {
        "id": pid,
        "name": "Новый проект",
        "created_at": now.isoformat(),
        "source_text": "",
        "master_prompt": _load_prompt("master"),
        "hero_prompt": "",
        "duration_minutes": 20,
        "chars_per_minute": 700,
        "scene_duration_seconds": 6,
        "voice_language": "ru",
        "scene_style_prefix": "",
        "humanize_mode": "norm",
        "stages": {
            "pre_analysis":  {"prompt": _load_prompt("pre_analysis"),  "result": ""},
            "analysis":      {"prompt": _load_prompt("analysis"),      "result": ""},
            "structure":     {"prompt": _load_prompt("structure"),     "result": ""},
            "block_writer":  {"prompt": _load_prompt("block_writer"),  "result": "", "blocks": []},
            "merger":        {"prompt": _load_prompt("merger"),        "result": ""},
            "quality_check": {"prompt": _load_prompt("quality_check"), "result": ""},
            "final":         {"prompt": _load_prompt("final"),         "result": ""},
            "humanize_tts":  {"prompt": _load_prompt("humanize_tts"),  "result": ""},
            "scene_builder": {"prompt": _load_prompt("scene_builder"), "result": ""},
        },
    }
    _save(project)
    return jsonify({"id": pid})


@app.route("/project/<pid>")
def project_page(pid):
    project = _load(pid)
    if not project:
        return "Проект не найден", 404
    return render_template("project.html", project=project)


@app.route("/project/<pid>/save", methods=["POST"])
def save_project(pid):
    project = _load(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    data = request.json or {}
    for key in ("source_text", "master_prompt", "hero_prompt",
                "duration_minutes", "chars_per_minute", "scene_duration_seconds",
                "humanize_mode", "voice_language", "scene_style_prefix", "name"):
        if key in data:
            project[key] = data[key]
    if "stages" in data:
        for stage_key, stage_data in data["stages"].items():
            if stage_key in project["stages"]:
                for field in ("prompt", "result", "blocks"):
                    if field in stage_data:
                        project["stages"][stage_key][field] = stage_data[field]
    _save(project)
    return jsonify({"ok": True})


@app.route("/project/<pid>/rename", methods=["POST"])
def rename_project(pid):
    project = _load(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    project["name"] = (request.json or {}).get("name", project["name"])
    _save(project)
    return jsonify({"ok": True})


@app.route("/project/<pid>/delete", methods=["POST"])
def delete_project(pid):
    p = _path(pid)
    if os.path.exists(p):
        os.remove(p)
    return jsonify({"ok": True})


@app.route("/prompt/<name>")
def get_prompt(name):
    allowed = {"pre_analysis","analysis","structure","block_writer",
               "merger","quality_check","final","humanize_tts","scene_builder","master"}
    if name not in allowed:
        return jsonify({"error": "not found"}), 404
    return jsonify({"prompt": _load_prompt(name)})


@app.route("/project/<pid>/export_scenes")
def export_scenes(pid):
    project = _load(pid)
    if not project:
        return "Not found", 404
    text = project["stages"].get("scene_builder", {}).get("result", "") or ""
    buf  = io.BytesIO(text.encode("utf-8"))
    safe = project["name"].replace(" ", "_")[:50]
    return send_file(buf, as_attachment=True,
                     download_name=f"{safe}_scenes.ndjson",
                     mimetype="application/x-ndjson")


@app.route("/project/<pid>/export_scenes_text")
def export_scenes_text(pid):
    """Export only the text lines from scene NDJSON — one line per scene for ElevenLabs."""
    project = _load(pid)
    if not project:
        return "Not found", 404
    ndjson = project["stages"].get("scene_builder", {}).get("result", "") or ""
    texts = []
    for line in ndjson.split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if "text" in obj and isinstance(obj["text"], str) and obj["text"].strip():
                texts.append(obj["text"].strip())
        except Exception:
            continue
    out = "\n".join(texts)
    buf  = io.BytesIO(out.encode("utf-8"))
    safe = project["name"].replace(" ", "_")[:50]
    return send_file(buf, as_attachment=True,
                     download_name=f"{safe}_tts_text.txt",
                     mimetype="text/plain")


@app.route("/project/<pid>/export_tts")
def export_tts(pid):
    project = _load(pid)
    if not project:
        return "Not found", 404
    text = project["stages"].get("humanize_tts", {}).get("result", "") or ""
    buf  = io.BytesIO(text.encode("utf-8"))
    safe = project["name"].replace(" ", "_")[:50]
    return send_file(buf, as_attachment=True,
                     download_name=f"{safe}_elevenlabs.txt",
                     mimetype="text/plain")


@app.route("/project/<pid>/export")
def export_project(pid):
    project = _load(pid)
    if not project:
        return "Not found", 404
    text = (project["stages"]["final"]["result"]
            or project["stages"]["merger"]["result"]
            or "\n\n---\n\n".join(project["stages"]["block_writer"].get("blocks", []))
            or "")
    buf = io.BytesIO(text.encode("utf-8"))
    safe = project["name"].replace(" ", "_")[:50]
    return send_file(buf, as_attachment=True,
                     download_name=f"{safe}.txt", mimetype="text/plain")


# ─── Run (streaming NDJSON) ───────────────────────────────────────────────────

@app.route("/project/<pid>/run", methods=["POST"])
def run_stage(pid):
    project = _load(pid)
    if not project:
        return jsonify({"error": "not found"}), 404

    data = request.json or {}
    stage = data.get("stage")

    def emit(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False) + "\n"

    def generate():
        try:
            api_key = os.getenv("OPENAI_API_KEY", "")
            if not api_key:
                yield emit({"type": "error", "message": "OPENAI_API_KEY не задан. Добавьте его в .env"})
                return

            proxy_url = os.getenv("OPENAI_PROXY", "")
            if proxy_url:
                import httpx as _httpx
                _http_client = _httpx.Client(proxy=proxy_url, timeout=300)
                client = openai.OpenAI(api_key=api_key, http_client=_http_client, timeout=300)
            else:
                client = openai.OpenAI(api_key=api_key, timeout=300)
            model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
            temperature = float(STAGE_TEMPERATURES.get(stage, 0.4))

            dur_hint = (
                f"Длительность видео: {project['duration_minutes']} мин., "
                f"целевой объём: {_target_total(project)} символов."
            )

            # ── Build messages ──────────────────────────────────────────────
            _hero = project.get("hero_prompt", "")

            if stage == "pre_analysis":
                system = "\n\n".join(filter(None, [
                    project["master_prompt"],
                    _hero,
                    project["stages"]["pre_analysis"]["prompt"],
                ]))
                user = project["source_text"]

            elif stage == "analysis":
                system = "\n\n".join(filter(None, [
                    project["master_prompt"],
                    _hero,
                    project["stages"]["analysis"]["prompt"],
                    dur_hint,
                ]))
                pre = project["stages"].get("pre_analysis", {}).get("result", "")
                user = (
                    f"ИСХОДНЫЙ ТЕКСТ:\n{project['source_text']}\n\n"
                    f"PRE-ANALYSIS RESULT:\n{pre}"
                )

            elif stage == "structure":
                system = "\n\n".join(filter(None, [
                    project["master_prompt"],
                    _hero,
                    project["stages"]["structure"]["prompt"],
                    dur_hint,
                ]))
                user = (
                    f"PRE-ANALYSIS RESULT:\n{project['stages'].get('pre_analysis', {}).get('result', '')}\n\n"
                    f"ANALYSIS RESULT:\n{project['stages']['analysis']['result']}"
                )

            elif stage == "block_writer":
                block_index   = data.get("block_index", 0)
                block_data    = data.get("block_data", {})
                source_segs   = data.get("source_segments", "")
                total_blocks  = data.get("total_blocks", 1)
                last_tail     = data.get("last_block_tail", "")

                system = "\n\n".join(filter(None, [
                    project["master_prompt"],
                    project.get("hero_prompt", ""),
                    project["stages"]["block_writer"]["prompt"],
                ]))
                user = (
                    f"Блок {block_index + 1} из {total_blocks}: \"{block_data.get('block_name', '')}\"\n"
                    f"Роль: {block_data.get('block_role', '')}\n"
                    f"Задача: {block_data.get('purpose', '')}\n"
                    f"Обязательно включить: {', '.join(block_data.get('must_cover', []))}\n"
                    f"Тип retention hook: {block_data.get('hook_type', '')}\n"
                    f"Заметки: {block_data.get('notes', '')}\n"
                    f"Целевой объём: {block_data.get('target_chars', 300)} символов (±50)\n\n"
                    f"Что было в предыдущем блоке (последние слова):\n{last_tail}\n\n"
                    f"Следующий блок будет о: {block_data.get('next_block_hint', '')}\n\n"
                    f"Исходные сегменты для этого блока:\n{source_segs}"
                )

            elif stage == "merger":
                blocks = project["stages"]["block_writer"].get("blocks", []) or []
                # Guardrail: do not allow merger to run on empty blocks
                if not any((b or "").strip() for b in blocks):
                    yield emit({"type": "error", "message": "Merger: нет готовых блоков. Сначала запустите Block Writer."})
                    return
                blocks_text = "\n\n---\n\n".join(
                    f"[Блок {i+1}]\n{b}" for i, b in enumerate(blocks) if b
                )
                system = "\n\n".join(filter(None, [
                    project["master_prompt"],
                    _hero,
                    project["stages"]["merger"]["prompt"],
                ]))
                user = (
                    f"ОРИГИНАЛ:\n{project['source_text']}\n\n"
                    f"ГОТОВЫЕ БЛОКИ (по порядку):\n{blocks_text}"
                )

            elif stage == "quality_check":
                system = "\n\n".join(filter(None, [
                    project["master_prompt"],
                    _hero,
                    project["stages"]["quality_check"]["prompt"],
                    dur_hint,
                ]))
                user = (
                    f"ОРИГИНАЛ:\n{project['source_text']}\n\n"
                    f"MERGER RESULT:\n{project['stages']['merger']['result']}\n\n"
                    f"TARGET_CHARS: {_target_total(project)}"
                )

            elif stage == "final":
                system = "\n\n".join(filter(None, [
                    project["master_prompt"],
                    _hero,
                    project["stages"]["final"]["prompt"],
                    dur_hint,
                ]))
                user = (
                    f"MERGER RESULT:\n{project['stages']['merger']['result']}\n\n"
                    f"QUALITY CHECK RESULT:\n{project['stages'].get('quality_check', {}).get('result', '')}"
                )

            elif stage == "humanize_tts":
                final_text = (project["stages"].get("final", {}).get("result", "") or "").strip()
                if not final_text:
                    yield emit({"type": "error", "message": "Humanize TTS: результат этапа Final пустой"})
                    return
                mode = (data.get("humanize_mode") or project.get("humanize_mode") or "norm").strip().lower()
                if mode not in ("min", "norm", "max"):
                    mode = "norm"
                mode_hint = {
                    "min": (
                        "Режим MIN: лёгкая обработка. Минимум тегов и мягкая пунктуация. "
                        "Ориентир: 1 тег на 3-4 предложения."
                    ),
                    "norm": (
                        "Режим NORM: сбалансированная выразительность. "
                        "Ориентир: 1 тег на 1-2 предложения."
                    ),
                    "max": (
                        "Режим MAX: максимально выразительная подача в рамках смысла. "
                        "Больше маркеров и интонационной пунктуации, но без потери читабельности."
                    ),
                }[mode]
                system = "\n\n".join(filter(None, [
                    project["master_prompt"],
                    _hero,
                    project["stages"]["humanize_tts"]["prompt"],
                ]))
                user = (
                    "Подготовь текст для ElevenLabs.\n"
                    "Сохрани факты и структуру, но сделай подачу живой.\n\n"
                    f"{mode_hint}\n\n"
                    f"ТЕКСТ:\n{final_text}"
                )

            elif stage == "scene_builder":
                # ── Scene Builder: smart-split + video distribution ───────────
                import random as _random

                _CHARS_PER_MINUTE_VOICE = {"ru": 850, "en": 700}
                voice_language  = (data.get("voice_language") or
                                   project.get("voice_language", "ru"))
                scene_dur       = int(data.get("scene_duration_seconds",
                                               project.get("scene_duration_seconds", 6)))
                chars_per_min   = _CHARS_PER_MINUTE_VOICE.get(voice_language, 850)
                chars_per_scene = round(scene_dur * chars_per_min / 60)
                scene_style_prefix = (project.get("scene_style_prefix") or "").strip()
                raw_prompt      = project["stages"]["scene_builder"]["prompt"]
                filled_prompt   = (raw_prompt
                                   .replace("{scene_duration_seconds}", str(scene_dur))
                                   .replace("{chars_per_scene}", str(chars_per_scene)))
                final_text = (project["stages"]["final"]["result"] or "").strip()
                if not final_text:
                    yield emit({"type": "error",
                                "message": "Scene Builder: результат этапа Final пустой"})
                    return

                import re as _re

                # ── Preprocess: strip timing markers like [0:00], [1:30] ──────
                def _strip_timecodes(text):
                    text = _re.sub(r'\[\d+:\d+\]\s*', '', text)
                    lines = [l.strip() for l in text.split('\n') if l.strip()]
                    return ' '.join(lines)

                final_text = _strip_timecodes(final_text)

                # ── Smart semantic scene splitter ──────────────────────────────
                def smart_split_into_scenes(text, target_chars, vl="ru"):
                    sentences = _re.split(r'(?<=[.!?])\s+', text.strip())
                    sentences = [s.strip() for s in sentences if s.strip()]
                    min_c = int(target_chars * 0.5)
                    max_c = int(target_chars * 1.2)
                    scenes, current = [], ""
                    for sent in sentences:
                        if not current:
                            current = sent; continue
                        combined = current + " " + sent
                        if len(combined) <= max_c:
                            current = combined
                        else:
                            scenes.append(current.strip())
                            current = sent
                    if current.strip():
                        scenes.append(current.strip())
                    # Merge scenes that are too short
                    merged, i = [], 0
                    while i < len(scenes):
                        if len(scenes[i]) < min_c and i + 1 < len(scenes):
                            merged.append(scenes[i] + " " + scenes[i + 1])
                            i += 2
                        else:
                            merged.append(scenes[i])
                            i += 1
                    return merged

                # ── Duration & speed helpers ───────────────────────────────────
                def calc_duration(text, vl="ru"):
                    cpm = _CHARS_PER_MINUTE_VOICE.get(vl, 850)
                    return max(1.0, round(len(text.strip()) / cpm * 60, 1))

                def get_speed_tag(duration, target):
                    ratio = duration / max(target, 0.1)
                    if ratio < 0.7:   return "[slowly] "
                    if ratio > 1.4:   return "[fast] "
                    return ""

                # ── NDJSON helpers ─────────────────────────────────────────────
                def _norm_prompt_val(v):
                    if v is None: return None
                    if not isinstance(v, str): return None
                    s = v.strip()
                    return None if (not s or s.lower() == "null") else s

                def _with_style(prompt: str) -> str:
                    """Prepend scene_style_prefix to a prompt if set."""
                    if not scene_style_prefix or not prompt:
                        return prompt
                    return f"{scene_style_prefix}, {prompt}"

                def _explode_json_candidates(raw):
                    candidates = []
                    for line in raw.split('\n'):
                        line = line.strip()
                        if not line: continue
                        for p in _re.split(r'(?<=\})\s*(?=\{)', line):
                            p = p.strip()
                            if p: candidates.append(p)
                    return candidates

                def _parse_presplit_batch(raw, pre_texts_dict):
                    """
                    Parse 4-line NDJSON (scene_id + start + end + video).
                    Text is taken from pre_texts_dict[scene_id].
                    """
                    objs = []
                    for cand in _explode_json_candidates(raw):
                        try: objs.append(json.loads(cand))
                        except Exception: continue

                    scenes, cur = [], None

                    def _build_end_prompt(start_prompt: str, text: str) -> str:
                        """
                        Ensure end prompt is always present.
                        Keep it as a final-frame continuation of the same scene.
                        """
                        base = (start_prompt or "").strip()
                        if base:
                            return (
                                f"{base.rstrip('.')} , final frame of the same scene, "
                                "state after the action settles, flat 2D animation style"
                            )
                        return (
                            "Flat 2D stick-figure final frame of the same scene, "
                            f"showing the end state after: {text[:180].replace(chr(10), ' ')}"
                        )

                    def _fin(scene):
                        if not scene: return
                        sid  = str(scene.get("scene_id") or "").strip()
                        text = pre_texts_dict.get(sid, "").strip()
                        if not text: return
                        sp = _norm_prompt_val((scene.get("start") or {}).get("prompt"))
                        if sp is None:
                            sp = ("Flat 2D stick-figure scene, bold outlines, illustrate: "
                                  + text[:200].replace('\n', ' '))
                        ep = _norm_prompt_val((scene.get("end") or {}).get("prompt"))
                        if ep is None:
                            ep = _build_end_prompt(sp, text)
                        scenes.append({
                            "scene_id": sid,
                            "text": text,
                            "start": {"prompt": _with_style(sp)},
                            "end":   {"prompt": _with_style(ep)},
                            "video": {"prompt": _norm_prompt_val((scene.get("video") or {}).get("prompt"))},
                        })

                    for obj in objs:
                        if "scene_id" in obj:
                            _fin(cur)
                            cur = {"scene_id": obj["scene_id"],
                                   "start": {"prompt": None},
                                   "end":   {"prompt": None},
                                   "video": {"prompt": None}}
                        elif cur is None: continue
                        elif "start" in obj and isinstance(obj.get("start"), dict):
                            cur["start"] = {"prompt": obj["start"].get("prompt")}
                        elif "end" in obj and isinstance(obj.get("end"), dict):
                            cur["end"] = {"prompt": obj["end"].get("prompt")}
                        elif "video" in obj and isinstance(obj.get("video"), dict):
                            cur["video"] = {"prompt": obj["video"].get("prompt")}
                        # also handle full 5-line format (text line present)
                        elif "text" in obj and cur:
                            cur.setdefault("_text_override", obj["text"])
                    _fin(cur)
                    return scenes

                STATIC_FRAME_SECONDS = float(project.get("frame_seconds_static", 3.0))

                def _serialize_scenes_ndjson(scenes, vl="ru", target_dur=6.0):
                    out = []
                    for i, sc in enumerate(scenes, start=1):
                        sid       = f"scene_{i:03d}"
                        text      = sc["text"]
                        dur       = calc_duration(text, vl)
                        stag      = get_speed_tag(dur, target_dur)
                        has_video = sc["video"]["prompt"] is not None
                        # frame_seconds: seconds each static frame is shown (start + end).
                        # null when video is present (video handles timing).
                        frame_s   = None if has_video else STATIC_FRAME_SECONDS
                        out.append(json.dumps({"scene_id": sid},                             ensure_ascii=False))
                        out.append(json.dumps({"text": text},                                ensure_ascii=False))
                        out.append(json.dumps({"start": {"prompt": sc["start"]["prompt"]}},  ensure_ascii=False))
                        out.append(json.dumps({"end":   {"prompt": sc["end"]["prompt"]}},    ensure_ascii=False))
                        out.append(json.dumps({"video": {"prompt": sc["video"]["prompt"]}},  ensure_ascii=False))
                        out.append(json.dumps({"duration_seconds": dur},                     ensure_ascii=False))
                        out.append(json.dumps({"speed_tag": stag},                           ensure_ascii=False))
                        out.append(json.dumps({"frame_seconds": frame_s},                    ensure_ascii=False))
                    return "\n".join(out)

                # ── Video distribution helpers ──────────────────────────────────
                def _scene_zone(idx, total):
                    pos = idx / max(total, 1)
                    if pos < 0.33:   return "BEGINNING", 0.72
                    elif pos < 0.67: return "MIDDLE",    0.35
                    else:            return "END",        0.12

                def _video_flags(scene_start, n, total):
                    return [(_random.random() < _scene_zone(scene_start + i, total)[1],
                             _scene_zone(scene_start + i, total)[0])
                            for i in range(n)]

                # ── Pre-split text into semantic scenes ────────────────────────
                pre_scenes   = smart_split_into_scenes(final_text, chars_per_scene, voice_language)
                total_scenes_est = max(len(pre_scenes), 1)

                # Map scene_id → text for LLM result lookup
                pre_texts_dict = {}
                for gi, txt in enumerate(pre_scenes):
                    pre_texts_dict[f"scene_{gi + 1:03d}"] = txt

                # Group into batches of 20 scenes for LLM calls
                BATCH_SIZE = 20
                batches  = [pre_scenes[i:i + BATCH_SIZE]
                             for i in range(0, len(pre_scenes), BATCH_SIZE)]
                n_chunks = len(batches)

                all_scenes = []
                scene_num  = 1  # global 1-based counter
                total_dropped_empty = 0

                for ci, batch_scene_texts in enumerate(batches):
                    n_in_chunk = len(batch_scene_texts)
                    flags = _video_flags(scene_num - 1, n_in_chunk, total_scenes_est)

                    # Build per-scene video instructions
                    video_lines = []
                    for k, (gen, zone) in enumerate(flags):
                        sn = scene_num + k
                        if gen:
                            video_lines.append(f"scene_{sn:03d}: GENERATE_VIDEO=true, zone={zone}")
                        else:
                            video_lines.append(f"scene_{sn:03d}: GENERATE_VIDEO=false")
                    video_instruction = "VIDEO DISTRIBUTION FOR THIS BATCH:\n" + "\n".join(video_lines)

                    # Format pre-split scenes for the LLM
                    scene_lines = []
                    for k, stxt in enumerate(batch_scene_texts):
                        sn = scene_num + k
                        scene_lines.append(f'scene_{sn:03d}: {stxt}')
                    scenes_block = "\n".join(scene_lines)

                    yield emit({"type": "status",
                                "message": (f"Часть {ci+1}/{n_chunks} "
                                            f"— сцены {scene_num}–{scene_num + n_in_chunk - 1} "
                                            f"из {total_scenes_est}...")})

                    chunk_system = filled_prompt
                    if scene_style_prefix:
                        chunk_system += (
                            f"\n\nSTYLE PREFIX (обязателен в каждом промпте):\n"
                            f"{scene_style_prefix}\n\n"
                            f"Каждый start.prompt и end.prompt ДОЛЖЕН начинаться с этого стиля. "
                            f"Не добавляй никаких других стилевых решений кроме заданного."
                        )
                    if scene_num > 1:
                        chunk_system += (
                            f"\n\nПродолжай нумерацию с scene_{scene_num:03d}. "
                            f"Первая сцена этой части = scene_{scene_num:03d}."
                        )

                    user_msg = (
                        f"{video_instruction}\n\n"
                        f"ГОТОВЫЕ СЦЕНЫ (текст зафиксирован, генерируй только визуальные промпты):\n"
                        f"{scenes_block}"
                    )

                    chunk_stream = client.chat.completions.create(
                        model=model,
                        temperature=temperature,
                        messages=[
                            {"role": "system", "content": chunk_system},
                            {"role": "user",   "content": user_msg},
                        ],
                        stream=True,
                    )

                    chunk_raw = ""
                    for c in chunk_stream:
                        delta = c.choices[0].delta.content
                        if delta:
                            chunk_raw += delta
                            yield emit({"type": "delta", "content": delta})

                    # Parse batch: use pre-split texts as authoritative source
                    batch_lookup = {f"scene_{scene_num + k:03d}": stxt
                                    for k, stxt in enumerate(batch_scene_texts)}
                    chunk_scenes = _parse_presplit_batch(chunk_raw, batch_lookup)

                    # Fallback: if LLM returned nothing for a scene, synthesise it
                    returned_ids = {sc["scene_id"] for sc in chunk_scenes}
                    for k, stxt in enumerate(batch_scene_texts):
                        sid = f"scene_{scene_num + k:03d}"
                        if sid not in returned_ids:
                            sp = ("Flat 2D stick-figure scene, bold outlines, illustrate: "
                                  + stxt[:200].replace('\n', ' '))
                            ep = (
                                f"{sp.rstrip('.')} , final frame of the same scene, "
                                "state after the action settles, flat 2D animation style"
                            )
                            chunk_scenes.append({"scene_id": sid, "text": stxt,
                                                 "start": {"prompt": _with_style(sp)},
                                                 "end":   {"prompt": _with_style(ep)},
                                                 "video": {"prompt": None}})
                            total_dropped_empty += 1

                    # Keep order consistent with pre-split
                    chunk_scenes.sort(key=lambda s: s["scene_id"])

                    all_scenes.extend(chunk_scenes)
                    scene_num = len(all_scenes) + 1
                    yield emit({
                        "type": "status",
                        "message": (f"Часть {ci+1}/{n_chunks}: {len(chunk_scenes)} сцен"),
                    })

                # Final renumber + serialize (7-line NDJSON with duration + speed_tag)
                full_content = _serialize_scenes_ndjson(
                    all_scenes, vl=voice_language, target_dur=float(scene_dur))
                yield emit({
                    "type": "status",
                    "message": (
                        f"Scene Builder: {len(all_scenes)} сцен, "
                        f"язык={voice_language}, {scene_dur}сек/сцена"
                    ),
                })

                # Persist and finish
                fresh = _load(pid)
                if fresh:
                    fresh["stages"]["scene_builder"]["result"] = full_content
                    _save(fresh)
                yield emit({"type": "result", "content": full_content})
                return  # skip common streaming code below

            else:
                yield emit({"type": "error", "message": f"Неизвестный этап: {stage}"})
                return

            yield emit({"type": "status", "message": "Отправляем запрос к OpenAI..."})

            stream = client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                stream=True,
            )

            full_content = ""
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    full_content += delta
                    yield emit({"type": "delta", "content": delta})

            # ── Post-processing ─────────────────────────────────────────────

            # JSON stages: retry up to 3 times if invalid JSON
            if stage in ("pre_analysis", "analysis", "structure", "quality_check"):
                parsed, err = _try_parse_json(full_content)
                if err:
                    yield emit({"type": "status", "message": f"⚠ JSON не парсится: {err}"})
                    retry_text = full_content
                    for attempt in (1, 2, 3):
                        yield emit({"type": "status", "message": f"↻ Повтор JSON-ответа ({attempt}/3)..."})
                        resp = client.chat.completions.create(
                            model=model,
                            temperature=temperature,
                            messages=[
                                {"role": "system", "content": system},
                                {"role": "user", "content": user + _json_retry_suffix(attempt)},
                            ],
                            stream=False,
                        )
                        retry_text = (resp.choices[0].message.content or "").strip()
                        parsed, err = _try_parse_json(retry_text)
                        if not err:
                            full_content = retry_text
                            yield emit({"type": "replace", "content": full_content})
                            yield emit({"type": "status", "message": "✓ JSON валиден"})
                            break
                    # if still invalid, keep original stream content (user will see error)

            # Structure: normalize target_chars so they sum to exact target
            if stage == "structure":
                target_total = _target_total(project)
                normalized, msg = _normalize_structure(full_content, target_total)
                if normalized is not None:
                    full_content = normalized
                yield emit({"type": "status", "message": msg})

            # Block Writer: retry if too short (< target - 100)
            if stage == "block_writer":
                target_chars = data.get("block_data", {}).get("target_chars", 0)
                actual = len(full_content)
                if target_chars and actual < (target_chars - 100):
                    yield emit({"type": "status", "message": f"⚠ Блок короткий: {actual} симв вместо {target_chars}. Повторяю..."})
                    tail = (full_content or "")[-200:]
                    resp = client.chat.completions.create(
                        model=model,
                        temperature=temperature,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": (
                                f"Текст слишком короткий ({actual} симв вместо {target_chars}).\n"
                                f"Допиши до {target_chars} символов, продолжи с места:\n{tail}\n\n"
                                f"Полный текст блока сейчас:\n{full_content}\n\n"
                                "Верни полный текст блока целиком (не только продолжение)."
                            )},
                        ],
                        stream=False,
                    )
                    corrected = (resp.choices[0].message.content or "").strip()
                    full_content = corrected
                    yield emit({"type": "replace", "content": corrected})
                    yield emit({"type": "status", "message": f"✓ После повтора: {len(corrected)} симв (цель {target_chars})"})

            # Final: enforce exact total length (strict)
            if stage == "final":
                target_total = _target_total(project)
                if target_total > 0:
                    fixed, msgs = _enforce_exact_length(
                        client,
                        model,
                        system=system,
                        text=full_content,
                        target_chars=target_total,
                        guidance=(
                            "Это финальная версия YouTube-скрипта с таймингами вида [0:00]. "
                            "Сохрани тайминги и естественный монолог. Не добавляй заголовки и пояснения."
                        ),
                        max_attempts=3,
                    )
                    if fixed != full_content:
                        yield emit({"type": "replace", "content": fixed})
                        full_content = fixed
                    for m in msgs:
                        yield emit({"type": "status", "message": m})

            # ── Persist result ──────────────────────────────────────────────
            fresh = _load(pid)
            if fresh:
                if stage == "block_writer":
                    block_index = data.get("block_index", 0)
                    blocks = fresh["stages"]["block_writer"].get("blocks", [])
                    while len(blocks) <= block_index:
                        blocks.append("")
                    blocks[block_index] = full_content
                    fresh["stages"]["block_writer"]["blocks"] = blocks
                else:
                    fresh["stages"][stage]["result"] = full_content
                _save(fresh)

            yield emit({"type": "result", "content": full_content})

        except openai.AuthenticationError:
            yield emit({"type": "error", "message": "Неверный OPENAI_API_KEY"})
        except openai.RateLimitError:
            yield emit({"type": "error", "message": "OpenAI: превышен rate limit"})
        except Exception as e:
            yield emit({"type": "error", "message": str(e)})

    return Response(
        stream_with_context(generate()),
        content_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ─── Transcription ────────────────────────────────────────────────────────────

LANG_NAMES = {
    "en": "English", "ru": "Русский", "de": "Deutsch", "fr": "Français",
    "es": "Español", "it": "Italiano", "pt": "Português", "zh": "中文",
    "ja": "日本語", "ko": "한국어", "ar": "العربية", "uk": "Українська",
    "pl": "Polski", "tr": "Türkçe", "nl": "Nederlands",
}


@app.route("/transcribe")
def transcribe_page():
    return render_template("transcribe.html")


@app.route("/transcribe/run", methods=["POST"])
def transcribe_run():
    data = request.json or {}
    url  = (data.get("url") or "").strip()

    def emit(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False) + "\n"

    def generate():
        tmp_dir  = None
        tmp_path = None
        try:
            api_key = os.getenv("OPENAI_API_KEY", "")
            if not api_key:
                yield emit({"type": "error", "message": "OPENAI_API_KEY не задан"})
                return
            if not url:
                yield emit({"type": "error", "message": "Укажите ссылку на YouTube"})
                return
            if not re.search(r'(youtube\.com|youtu\.be)', url):
                yield emit({"type": "error", "message": "Ссылка не похожа на YouTube URL"})
                return

            _px = os.getenv("OPENAI_PROXY", "")
            _http_client = __import__('httpx').Client(proxy=_px, timeout=300) if _px else None
            client = openai.OpenAI(api_key=api_key, http_client=_http_client, timeout=300)
            model  = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)

            # Resolve yt-dlp: prefer venv binary, then PATH
            _here = os.path.dirname(os.path.abspath(__file__))
            _ytdlp = os.path.join(_here, "venv", "bin", "yt-dlp")
            if not os.path.isfile(_ytdlp):
                import shutil
                _ytdlp = shutil.which("yt-dlp") or "yt-dlp"

            # Resolve node.js for JS challenge solving
            import shutil as _shutil
            _node = _shutil.which("node") or _shutil.which("nodejs") or "node"

            # ── Step 1: get video info ─────────────────────────────────────
            yield emit({"type": "step", "step": 1, "message": "Получаем информацию о видео..."})
            info_cmd = [
                _ytdlp, "--dump-json", "--no-playlist",
                "--cookies-from-browser", "chrome",
                "--js-runtimes", f"node:{_node}",
                "--remote-components", "ejs:github",
                url,
            ]
            info_res = subprocess.run(info_cmd, capture_output=True, text=True, timeout=60)
            video_title    = "Без названия"
            video_duration = 0
            if info_res.returncode == 0:
                try:
                    info = json.loads(info_res.stdout)
                    video_title    = info.get("title", "Без названия")
                    video_duration = int(info.get("duration", 0))
                except Exception:
                    pass
            yield emit({"type": "info", "title": video_title,
                        "duration": video_duration,
                        "message": f"Видео: {video_title} ({video_duration // 60}:{video_duration % 60:02d})"})

            # ── Step 2: download audio ─────────────────────────────────────
            yield emit({"type": "step", "step": 2, "message": "Скачиваем аудио..."})
            tmp_dir  = tempfile.mkdtemp()
            tmp_path = os.path.join(tmp_dir, "audio.%(ext)s")

            dl_cmd = [
                _ytdlp, "-x",
                "--audio-format", "mp3",
                "--audio-quality", "5",
                "-o", tmp_path,
                "--no-playlist",
                "--newline",
                "--cookies-from-browser", "chrome",
                "--js-runtimes", f"node:{_node}",
                "--remote-components", "ejs:github",
                url,
            ]
            proc = subprocess.Popen(dl_cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            output_lines = []
            for line in proc.stdout:
                line = line.strip()
                output_lines.append(line)
                if "[download]" in line and "%" in line:
                    m = re.search(r'(\d+\.?\d*)%', line)
                    pct = float(m.group(1)) if m else 0
                    yield emit({"type": "download_progress", "pct": pct,
                                "message": line})
                elif "[ffmpeg]" in line or "[ExtractAudio]" in line:
                    yield emit({"type": "step", "step": 2,
                                "message": "Конвертируем в MP3..."})
            proc.wait()
            if proc.returncode != 0:
                err_detail = next(
                    (l for l in reversed(output_lines) if "ERROR" in l),
                    "Ошибка скачивания. Проверьте ссылку."
                )
                yield emit({"type": "error", "message": err_detail})
                return

            # Find the downloaded file
            mp3_path = None
            for f in os.listdir(tmp_dir):
                if f.endswith(".mp3"):
                    mp3_path = os.path.join(tmp_dir, f)
                    break
            if not mp3_path:
                yield emit({"type": "error", "message": "MP3 файл не найден после скачивания"})
                return

            file_size_mb = os.path.getsize(mp3_path) / 1024 / 1024
            yield emit({"type": "step", "step": 2,
                        "message": f"Аудио скачано: {file_size_mb:.1f} МБ"})

            # ── Step 3: transcribe with Whisper ───────────────────────────
            WHISPER_LIMIT = 24 * 1024 * 1024  # 24 MB — safety margin below 25 MB API limit
            file_size = os.path.getsize(mp3_path)

            # Split into chunks if file exceeds Whisper limit
            audio_chunks = []
            if file_size > WHISPER_LIMIT:
                # Fixed chunk size for reliability on long videos: 12 minutes.
                # Smaller chunks reduce long hanging requests and transient 5xx errors.
                chunk_seconds = 12 * 60

                yield emit({"type": "step", "step": 3,
                            "message": f"Файл {file_size_mb:.1f} МБ — разбиваем на части по {chunk_seconds//60} мин..."})

                import shutil as _shutil2
                _ffmpeg = _shutil2.which("ffmpeg") or "ffmpeg"
                chunk_pattern = os.path.join(tmp_dir, "chunk_%03d.mp3")
                split_cmd = [
                    _ffmpeg, "-i", mp3_path,
                    "-f", "segment",
                    "-segment_time", str(chunk_seconds),
                    "-reset_timestamps", "1",
                    "-c", "copy",
                    chunk_pattern,
                    "-y", "-loglevel", "error",
                ]
                split_res = subprocess.run(split_cmd, capture_output=True, text=True)
                if split_res.returncode != 0:
                    yield emit({"type": "error", "message": f"Ошибка разбивки аудио: {split_res.stderr}"})
                    return

                for fname in sorted(os.listdir(tmp_dir)):
                    if fname.startswith("chunk_") and fname.endswith(".mp3"):
                        audio_chunks.append(os.path.join(tmp_dir, fname))

                if not audio_chunks:
                    yield emit({"type": "error", "message": "Не удалось разбить аудио на части"})
                    return
            else:
                audio_chunks = [mp3_path]

            # Transcribe all chunks
            yield emit({"type": "step", "step": 3,
                        "message": f"Транскрибируем через Whisper{'  ('+str(len(audio_chunks))+' части)' if len(audio_chunks) > 1 else ''}..."})

            original_text = ""
            detected_lang = "unknown"
            for i, chunk_path in enumerate(audio_chunks):
                if len(audio_chunks) > 1:
                    chunk_mb = os.path.getsize(chunk_path) / 1024 / 1024
                    yield emit({"type": "step", "step": 3,
                                "message": f"Часть {i+1}/{len(audio_chunks)} ({chunk_mb:.1f} МБ)..."})
                chunk_transcript = None
                last_err = None
                for attempt in range(1, 4):
                    try:
                        with open(chunk_path, "rb") as f:
                            chunk_transcript = client.audio.transcriptions.create(
                                model="whisper-1",
                                file=f,
                                response_format="verbose_json",
                            )
                        last_err = None
                        break
                    except openai.AuthenticationError:
                        raise
                    except Exception as e:
                        last_err = e
                        msg = str(e)
                        is_retryable = (
                            "Error code: 500" in msg
                            or "Error code: 502" in msg
                            or "Error code: 503" in msg
                            or "Error code: 504" in msg
                            or "timed out" in msg.lower()
                            or "timeout" in msg.lower()
                        )
                        if attempt < 3 and is_retryable:
                            wait_s = attempt * 2
                            yield emit({
                                "type": "step",
                                "step": 3,
                                "message": (
                                    f"Whisper временно недоступен (попытка {attempt}/3). "
                                    f"Повтор через {wait_s}с..."
                                ),
                            })
                            time.sleep(wait_s)
                            continue
                        break

                if chunk_transcript is None:
                    raise last_err if last_err else RuntimeError("Whisper transcription failed")
                chunk_text = chunk_transcript.text or ""
                if i == 0:
                    detected_lang = getattr(chunk_transcript, "language", "unknown") or "unknown"
                if original_text and chunk_text:
                    original_text += " " + chunk_text
                else:
                    original_text += chunk_text

            lang_name = LANG_NAMES.get(detected_lang, detected_lang.upper())

            yield emit({"type": "transcript",
                        "text":     original_text,
                        "language": detected_lang,
                        "lang_name": lang_name,
                        "chars":    len(original_text),
                        "words":    len(original_text.split()),
                        "message":  f"Транскрибировано: {len(original_text.split())} слов, язык: {lang_name}"})

            # ── Step 4: translate if not Russian ──────────────────────────
            if detected_lang != "ru":
                yield emit({"type": "step", "step": 4,
                            "message": f"Переводим с {lang_name} на Русский..."})

                stream = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content":
                            "Переведи следующий текст на русский язык. "
                            "Сохраняй структуру абзацев, смысл и стиль. "
                            "Верни только перевод — без пояснений и комментариев."},
                        {"role": "user", "content": original_text},
                    ],
                    stream=True,
                    temperature=0.3,
                )
                translation = ""
                for chunk in stream:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        translation += delta
                        yield emit({"type": "translation_delta", "content": delta})

                yield emit({"type": "translation_done",
                            "text":  translation,
                            "chars": len(translation),
                            "words": len(translation.split())})
            else:
                yield emit({"type": "translation_done",
                            "text": original_text,
                            "chars": len(original_text),
                            "words": len(original_text.split()),
                            "same_language": True})

            yield emit({"type": "done", "message": "Готово!"})

        except subprocess.TimeoutExpired:
            yield emit({"type": "error", "message": "Таймаут скачивания (>30 сек). Проверьте ссылку."})
        except openai.AuthenticationError:
            yield emit({"type": "error", "message": "Неверный OPENAI_API_KEY"})
        except Exception as e:
            yield emit({"type": "error", "message": str(e)})
        finally:
            # Cleanup temp files
            if tmp_dir and os.path.exists(tmp_dir):
                for f in os.listdir(tmp_dir):
                    try: os.remove(os.path.join(tmp_dir, f))
                    except: pass
                try: os.rmdir(tmp_dir)
                except: pass

    return Response(
        stream_with_context(generate()),
        content_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ─── Translation (standalone) ─────────────────────────────────────────────────

TRANSLATE_LANGS = {
    "ru": "русский",
    "en": "English",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "it": "Italiano",
    "pt": "Português",
    "zh": "китайский",
    "ja": "японский",
    "ko": "корейский",
    "uk": "украинский",
    "pl": "Polish",
    "tr": "Türkçe",
    "nl": "Nederlands",
    "ar": "арабский",
}


@app.route("/translate/run", methods=["POST"])
def translate_run():
    data        = request.json or {}
    text        = (data.get("text") or "").strip()
    target_lang = (data.get("target_lang") or "ru").strip()

    def emit(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False) + "\n"

    def generate():
        try:
            api_key = os.getenv("OPENAI_API_KEY", "")
            if not api_key:
                yield emit({"type": "error", "message": "OPENAI_API_KEY не задан"})
                return
            if not text:
                yield emit({"type": "error", "message": "Нет текста для перевода"})
                return

            lang_name = TRANSLATE_LANGS.get(target_lang, target_lang)

            _px = os.getenv("OPENAI_PROXY", "")
            _http_client = __import__('httpx').Client(proxy=_px, timeout=300) if _px else None
            client = openai.OpenAI(api_key=api_key, http_client=_http_client, timeout=300)
            model  = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)

            stream = client.chat.completions.create(
                model=model,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": (
                        f"Переведи следующий текст на {lang_name}. "
                        "Сохраняй структуру абзацев, смысл и стиль. "
                        "Верни только перевод — без пояснений и комментариев."
                    )},
                    {"role": "user", "content": text},
                ],
                stream=True,
            )

            result = ""
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    result += delta
                    yield emit({"type": "delta", "content": delta})

            yield emit({
                "type":  "done",
                "text":  result,
                "chars": len(result),
                "words": len(result.split()),
            })

        except openai.AuthenticationError:
            yield emit({"type": "error", "message": "Неверный OPENAI_API_KEY"})
        except openai.RateLimitError:
            yield emit({"type": "error", "message": "OpenAI: превышен rate limit"})
        except Exception as e:
            yield emit({"type": "error", "message": str(e)})

    return Response(
        stream_with_context(generate()),
        content_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=True)
