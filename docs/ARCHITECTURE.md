# Architecture

## Purpose

ReWrite Master is a local, stage-driven script production system.  
It avoids single-shot prompt failure modes by decomposing generation into deterministic steps.

## High-Level Components

- **Flask server (`app.py`)**
  - Serves pages.
  - Manages project persistence.
  - Executes stage runs against OpenAI with NDJSON streaming.
- **Frontend (`templates/project.html`, `static/main.js`)**
  - Stage-by-stage control panel.
  - Per-stage prompt editing/locking.
  - Manual controls (stop buttons, mode selectors, export actions).
- **Prompt layer (`prompts/*.txt`)**
  - Stage-specific behavior contracts.
  - Easily adjustable without backend code changes.
- **Storage (`projects/*.json`)**
  - Full project snapshot, including stage prompts and results.

## Runtime Model

1. User creates project.
2. Source text + settings saved to local JSON.
3. Stage run triggers `POST /project/<id>/run`.
4. Backend streams model output as NDJSON events.
5. Frontend renders live output and persists stage result.
6. Export routes provide plain text or NDJSON artifacts.

## NDJSON Streaming Contract

Every streamed line is JSON with one of:

- `{"type":"status","message":"..."}`
- `{"type":"delta","content":"..."}`
- `{"type":"replace","content":"..."}`
- `{"type":"result","content":"..."}`
- `{"type":"error","message":"..."}`

`replace` is used when backend post-processing corrects previously streamed content.

## Persistence Model

Each project is one JSON file in `projects/` (runtime-only, git-ignored).

Main fields:

- `id`, `name`, `created_at`
- `source_text`
- `master_prompt`, `hero_prompt`
- `duration_minutes`, `chars_per_minute`
- `scene_duration_seconds`
- `humanize_mode` (`min|norm|max`)
- `stages` object with prompt/result per stage

## Stage Categories

- **Core rewrite stages:** `pre_analysis`, `analysis`, `structure`, `block_writer`, `merger`, `quality_check`, `final`
- **Manual post stages:** `humanize_tts`, `scene_builder`

Scene Builder output is treated as a strict handoff contract:
- `text` (exact scene narration)
- `start.prompt` (start frame)
- `end.prompt` (required end frame of the same scene)
- `video.prompt` (optional motion from start to end)

Manual stages are separate to enable controlled A/B testing and selective reruns.

## Error Handling Strategy

- Early errors for missing API key or missing upstream stage outputs.
- JSON stages retry up to 3 times when parse fails.
- Structure normalization enforces target char sum.
- Block writer retries if output is too short.
- Final stage enforces exact target length.

## Extension Points

- Add a stage:
  1. Create `prompts/<stage>.txt`
  2. Register in defaults (load/new project)
  3. Add prompt route allowlist
  4. Add run-case in `/run`
  5. Add UI card + JS wiring
  6. Add temperature in `config.py`
- Add an export artifact:
  - Create route + button in UI.

