import os
import json
import io
import re
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
                "duration_minutes", "chars_per_minute", "scene_duration_seconds", "humanize_mode", "name"):
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

            client = openai.OpenAI(api_key=api_key)
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
                # ── Scene Builder: chunked + video distribution ────────────────
                import random as _random

                scene_dur       = int(data.get("scene_duration_seconds",
                                               project.get("scene_duration_seconds", 6)))
                chars_per_min   = int(project.get("chars_per_minute", 700))
                chars_per_scene = round(scene_dur * chars_per_min / 60)
                raw_prompt      = project["stages"]["scene_builder"]["prompt"]
                filled_prompt   = (raw_prompt
                                   .replace("{scene_duration_seconds}", str(scene_dur))
                                   .replace("{chars_per_scene}", str(chars_per_scene)))
                final_text = (project["stages"]["final"]["result"] or "").strip()
                if not final_text:
                    yield emit({"type": "error",
                                "message": "Scene Builder: результат этапа Final пустой"})
                    return

                # ── Preprocess: strip timing markers like [0:00], [1:30] ──────
                import re as _re

                def _preprocess_final_text(text):
                    lines = text.split('\n')
                    clean_lines = []
                    for line in lines:
                        stripped = line.strip()
                        # Skip lines that are purely a timestamp
                        if _re.match(r'^\[\d+:\d+\]$', stripped):
                            continue
                        # Strip leading timestamp from lines that have content after it
                        cleaned = _re.sub(r'^\[\d+:\d+\]\s*', '', stripped)
                        if cleaned:
                            clean_lines.append(cleaned)
                    return '\n'.join(clean_lines)

                final_text = _preprocess_final_text(final_text)

                # ── NDJSON hygiene: parse, validate, normalize at scene level ───
                def _norm_prompt_val(v):
                    if v is None:
                        return None
                    if not isinstance(v, str):
                        return None
                    s = v.strip()
                    if not s:
                        return None
                    if s.lower() == "null":
                        return None
                    return s

                def _is_meaningful_text(s):
                    if not isinstance(s, str):
                        return False
                    t = s.strip()
                    if not t:
                        return False
                    # Ignore pure time marks accidentally emitted as scene text
                    if _re.match(r'^\[\d+:\d+\]$', t):
                        return False
                    return True

                def _explode_json_candidates(raw):
                    """
                    Split raw model output into JSON object candidates.
                    Handles glued objects like `}{` and line breaks.
                    """
                    candidates = []
                    for line in raw.split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        parts = _re.split(r'(?<=\})\s*(?=\{)', line)
                        for p in parts:
                            p = p.strip()
                            if p:
                                candidates.append(p)
                    return candidates

                def _parse_scene_blocks(raw):
                    """
                    Parse NDJSON-like scene stream into normalized scene dicts.
                    Scene shape:
                      {"scene_id": "...", "text": "...", "start":{"prompt":...}, "end":..., "video":...}
                    """
                    objs = []
                    for cand in _explode_json_candidates(raw):
                        try:
                            objs.append(json.loads(cand))
                        except Exception:
                            continue

                    scenes = []
                    cur = None
                    dropped_empty = 0

                    def _finalize(scene):
                        nonlocal dropped_empty
                        if not scene:
                            return
                        txt = (scene.get("text") or "").strip()
                        if not _is_meaningful_text(txt):
                            dropped_empty += 1
                            return

                        start_p = _norm_prompt_val(((scene.get("start") or {}).get("prompt")))
                        end_p = _norm_prompt_val(((scene.get("end") or {}).get("prompt")))
                        video_p = _norm_prompt_val(((scene.get("video") or {}).get("prompt")))

                        # Keep pipeline robust: if text exists but start prompt is absent,
                        # synthesize a safe fallback prompt instead of dropping content.
                        if start_p is None:
                            fallback = txt[:220].replace('\n', ' ')
                            start_p = (
                                "Flat 2D stick-figure scene, bold outlines, "
                                f"illustrate: {fallback}"
                            )

                        scenes.append({
                            "scene_id": str(scene.get("scene_id") or "").strip() or "scene_000",
                            "text": txt,
                            "start": {"prompt": start_p},
                            "end": {"prompt": end_p},
                            "video": {"prompt": video_p},
                        })

                    for obj in objs:
                        if "scene_id" in obj:
                            _finalize(cur)
                            cur = {
                                "scene_id": obj.get("scene_id"),
                                "text": "",
                                "start": {"prompt": None},
                                "end": {"prompt": None},
                                "video": {"prompt": None},
                            }
                            continue
                        if cur is None:
                            continue
                        if "text" in obj:
                            cur["text"] = obj.get("text")
                        if "start" in obj and isinstance(obj.get("start"), dict):
                            cur["start"] = {"prompt": obj["start"].get("prompt")}
                        if "end" in obj and isinstance(obj.get("end"), dict):
                            cur["end"] = {"prompt": obj["end"].get("prompt")}
                        if "video" in obj and isinstance(obj.get("video"), dict):
                            cur["video"] = {"prompt": obj["video"].get("prompt")}

                    _finalize(cur)
                    return scenes, dropped_empty

                def _serialize_scenes_ndjson(scenes):
                    out_lines = []
                    for i, sc in enumerate(scenes, start=1):
                        sid = f"scene_{i:03d}"
                        out_lines.append(json.dumps({"scene_id": sid}, ensure_ascii=False))
                        out_lines.append(json.dumps({"text": sc["text"]}, ensure_ascii=False))
                        out_lines.append(json.dumps({"start": {"prompt": sc["start"]["prompt"]}}, ensure_ascii=False))
                        out_lines.append(json.dumps({"end": {"prompt": sc["end"]["prompt"]}}, ensure_ascii=False))
                        out_lines.append(json.dumps({"video": {"prompt": sc["video"]["prompt"]}}, ensure_ascii=False))
                    return "\n".join(out_lines)

                # ── Video distribution helpers ──────────────────────────────────
                def _scene_zone(idx, total):
                    pos = idx / max(total, 1)
                    if pos < 0.33:   return "BEGINNING", 0.72
                    elif pos < 0.67: return "MIDDLE",    0.35
                    else:            return "END",        0.12

                def _video_flags_for_chunk(scene_start, n_scenes_in_chunk, total_scenes):
                    """Return list of (generate: bool, zone: str) per scene in chunk."""
                    flags = []
                    for i in range(n_scenes_in_chunk):
                        zone, prob = _scene_zone(scene_start + i, total_scenes)
                        flags.append((_random.random() < prob, zone))
                    return flags

                # ── Split text into ~2500-char chunks at sentence boundaries ────
                def _split_chunks(text, size=2500):
                    parts = []
                    while text:
                        if len(text) <= size:
                            parts.append(text); break
                        cut = size
                        for sep in ('. ', '! ', '? ', '\n'):
                            pos = text.rfind(sep, size // 2, size)
                            if pos != -1:
                                cut = pos + len(sep); break
                        parts.append(text[:cut])
                        text = text[cut:]
                    return parts

                chunks = _split_chunks(final_text)
                n_chunks = len(chunks)

                # Estimate total scenes across all chunks for zone calculation
                total_scenes_est = max(1, len(final_text) // max(chars_per_scene, 1))

                all_scenes = []
                scene_num  = 1  # target id for next chunk start
                total_dropped_empty = 0

                for ci, chunk_text in enumerate(chunks):
                    # How many scenes expected in this chunk
                    n_in_chunk = max(1, len(chunk_text) // max(chars_per_scene, 1))

                    # Build video instruction for this chunk
                    flags = _video_flags_for_chunk(
                        scene_num - 1, n_in_chunk, total_scenes_est
                    )
                    video_lines = []
                    for k, (gen, zone) in enumerate(flags):
                        sn = scene_num + k
                        if gen:
                            video_lines.append(f"scene_{sn:03d}: GENERATE_VIDEO=true, zone={zone}")
                        else:
                            video_lines.append(f"scene_{sn:03d}: GENERATE_VIDEO=false")
                    video_instruction = "VIDEO DISTRIBUTION FOR THIS CHUNK:\n" + "\n".join(video_lines)

                    yield emit({"type": "status",
                                "message": (f"Часть {ci+1}/{n_chunks} "
                                            f"— сцены с scene_{scene_num:03d}...")})

                    chunk_system = filled_prompt
                    if scene_num > 1:
                        chunk_system += (
                            f"\n\nПродолжай нумерацию с scene_{scene_num:03d}. "
                            f"Первая сцена этой части = scene_{scene_num:03d}."
                        )

                    user_msg = (
                        f"{video_instruction}\n\n"
                        f"ТЕКСТ:\n{chunk_text}"
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

                    # Parse + sanitize chunk scenes
                    chunk_scenes, dropped_empty = _parse_scene_blocks(chunk_raw)
                    total_dropped_empty += dropped_empty
                    all_scenes.extend(chunk_scenes)
                    scene_num = len(all_scenes) + 1
                    yield emit({
                        "type": "status",
                        "message": (
                            f"Часть {ci+1}/{n_chunks}: валидных сцен {len(chunk_scenes)}, "
                            f"отброшено пустых {dropped_empty}"
                        ),
                    })

                # Final renumber + serialize
                full_content = _serialize_scenes_ndjson(all_scenes)
                yield emit({
                    "type": "status",
                    "message": (
                        f"Scene Builder sanitize: итог {len(all_scenes)} сцен, "
                        f"удалено пустых {total_dropped_empty}"
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

            client = openai.OpenAI(api_key=api_key)
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
                # Calculate chunk duration: aim for ~20 MB chunks
                # At audio quality 5 (~80 kbps), 1 min ≈ 0.6 MB → 20 MB ≈ 33 min, use 20 min chunks
                chunk_seconds = 20 * 60  # 20 minutes per chunk
                n_chunks = max(2, int(file_size / WHISPER_LIMIT) + 1)
                chunk_seconds = max(600, (video_duration or 1800) // n_chunks)

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
                with open(chunk_path, "rb") as f:
                    chunk_transcript = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=f,
                        response_format="verbose_json",
                    )
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

            client = openai.OpenAI(api_key=api_key)
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
