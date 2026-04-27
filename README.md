<<<<<<< HEAD
# yout2
=======
# ReWrite Master

Local web app for long-form YouTube script rewriting, polishing, scene generation, and TTS-ready normalization.

The project is designed for iterative content production where each stage is explicit, inspectable, and editable.

## What This Project Does

- Takes a source script/transcript and rewrites it into a new narrative.
- Uses a multi-stage pipeline instead of one huge prompt (to reduce context loss).
- Generates scene-level NDJSON for downstream visual teams/tools.
- Produces an ElevenLabs-ready text pass with expressive punctuation and speech markers.
- Stores project state on disk as JSON so every stage remains reproducible.

## Core Pipeline

1. `pre_analysis` - extracts narrative leverage points.
2. `analysis` - maps source into structured segments/facts/emotion hints.
3. `structure` - creates block plan with target character budgets.
4. `block_writer` - writes each block in a loop (chunk-safe).
5. `merger` - stitches blocks into one coherent script.
6. `quality_check` - validates fidelity/retention/length constraints.
7. `final` - final script formatting and timing alignment.
8. `humanize_tts` (manual) - ElevenLabs-style expressive pass.
9. `scene_builder` (manual) - NDJSON scene export with image/video prompts.

`Run All` includes the main rewrite pipeline. `humanize_tts` and `scene_builder` are intentionally manual for controlled testing.

## Tech Stack

- Backend: Flask (`app.py`)
- Frontend: HTML/CSS/Vanilla JS (`templates/`, `static/`)
- LLM: OpenAI Chat Completions (`gpt-4.1` by default)
- Storage: local JSON files in `projects/`

## Quick Start

### 1) Install

```bash
pip install -r requirements.txt
```

### 2) Configure

Create `.env`:

```env
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-4.1
```

### 3) Run

```bash
python app.py
```

Open: `http://localhost:5002`

## Repository Structure

```text
rewrite-app/
├── app.py
├── config.py
├── requirements.txt
├── prompts/
├── static/
├── templates/
├── projects/              # runtime data, ignored by git
└── docs/
    ├── AI_ONBOARDING.md
    ├── ARCHITECTURE.md
    ├── API.md
    └── PIPELINE.md
```

## Documentation Map

- `docs/AI_ONBOARDING.md` - fastest way for an AI/code agent to understand and extend the app.
- `docs/ARCHITECTURE.md` - system architecture, data model, runtime behavior.
- `docs/PIPELINE.md` - exact stage contracts and flow.
- `docs/API.md` - HTTP endpoints and stream protocol details.

## Security and Local Data

- `.env` is ignored.
- `projects/` is runtime data and ignored by git.
- No credentials should be committed.

## Notes for Contributors

- Stage prompts live in `prompts/*.txt`.
- Stage temperatures live in `config.py`.
- Streaming is NDJSON (`type=status|delta|replace|result|error`).
- Keep manual stages manual unless product decision changes.
>>>>>>> 78d51fe (Initialize ReWrite Master with full pipeline and docs)
