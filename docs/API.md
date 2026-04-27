# API Reference

Base URL (local): `http://localhost:5002`

## Pages

- `GET /` - project list
- `GET /project/<pid>` - project workspace
- `GET /transcribe` - transcription page

## Project CRUD

- `POST /project/new` - create project
- `POST /project/<pid>/save` - persist mutable project fields
- `POST /project/<pid>/rename` - rename project
- `POST /project/<pid>/delete` - delete project JSON

## Prompt Retrieval

- `GET /prompt/<name>`
  - allowed names are backend-validated
  - returns `{ "prompt": "..." }`

## Stage Execution

- `POST /project/<pid>/run`
  - body: `{ "stage": "<stage_name>", ...stage_specific }`
  - response: `application/x-ndjson` stream

### Stage-specific request keys

- `block_writer`:
  - `block_index`
  - `block_data`
  - `source_segments`
  - `last_block_tail`
  - `total_blocks`
- `scene_builder`:
  - `scene_duration_seconds`
  - `chars_per_scene` (advisory)
- `humanize_tts`:
  - `humanize_mode` = `min|norm|max`

### Stream event schema

- `status`:
  - `{ "type":"status", "message":"..." }`
- `delta`:
  - `{ "type":"delta", "content":"..." }`
- `replace`:
  - `{ "type":"replace", "content":"..." }`
- `result`:
  - `{ "type":"result", "content":"..." }`
- `error`:
  - `{ "type":"error", "message":"..." }`

## Exports

- `GET /project/<pid>/export` - final script text
- `GET /project/<pid>/export_scenes` - scene NDJSON
- `GET /project/<pid>/export_tts` - humanized TTS text

## Transcription and Translation

- `POST /transcribe/run` - YouTube audio extraction + Whisper + optional translation
- `POST /translate/run` - standalone text translation

Both endpoints stream NDJSON updates similarly to stage runs.

