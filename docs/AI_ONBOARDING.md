# AI Onboarding Guide

This guide is for any AI/code agent joining the repository cold.

## 1) Mental Model

This is a **local content pipeline app** where each LLM step is explicit, persisted, and rerunnable.

Do not treat this as a simple prompt playground:

- stage ordering matters
- stage outputs are contracts for downstream stages
- manual stages (`humanize_tts`, `scene_builder`) are intentionally decoupled from `Run All`

## 2) Key Files You Must Read First

1. `app.py` - full backend logic and stage execution
2. `static/main.js` - frontend runtime behavior, streaming parser, controls
3. `templates/project.html` - UI structure and stage cards
4. `config.py` - model defaults and stage temperatures
5. `prompts/*.txt` - behavior contracts for each stage

## 3) Data Contracts to Respect

- Project JSON is the source of truth.
- Stage outputs are persisted under `project.stages[stage].result`.
- `block_writer` additionally uses `project.stages.block_writer.blocks[]`.
- Streaming protocol is NDJSON with typed events.

If you add/rename a stage, update **all** of:

- default project schema
- prompt allowlist route
- run-stage router branch
- frontend render list
- UI stage card
- temperature map

## 4) Typical Safe Change Workflow

1. Adjust prompt text first if behavior issue is prompt-level.
2. Only then modify backend enforcement logic if needed.
3. Keep frontend/backed stage naming exactly aligned.
4. Validate with:
   - one single-stage run
   - one full pipeline run
   - manual `humanize_tts` and `scene_builder` runs

## 5) Known Product Priorities

- Avoid context-loss on long text.
- Avoid malformed JSON from analysis/structure stages.
- Avoid dropped middle content in scene generation.
- Keep downstream handoff format stable (especially NDJSON scenes).
- Keep TTS pass controllable via mode (`min|norm|max`).

## 6) Common Failure Points

- Forgetting to persist new project-level settings in `/save`.
- Forgetting to backfill old projects in `_load`.
- UI showing stage that backend cannot run (or vice versa).
- Breaking stream parser by changing event schema.
- Introducing nullable/string mismatch (`null` vs `"null"`).

## 7) Coding Conventions in This Repo

- Keep business logic in backend; keep frontend focused on orchestration/UI state.
- Prefer additive changes over destructive refactors.
- Preserve manual controls for production-critical stages.
- Treat prompts as first-class configurable artifacts.

