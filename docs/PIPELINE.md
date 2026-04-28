# Pipeline Specification

This file defines input/output contracts for each stage.

## Stage 1: `pre_analysis`

- **Input:** `source_text`
- **Output:** JSON pre-analysis map (angles, leverage points)
- **Goal:** establish narrative strategy before granular segmentation

## Stage 2: `analysis`

- **Input:** source text + `pre_analysis` result
- **Output:** JSON segment map with importance/facts/emotion hints
- **Goal:** preserve factual core while enabling style transformation

## Stage 3: `structure`

- **Input:** `pre_analysis` + `analysis`
- **Output:** JSON blocks with:
  - `target_chars`
  - `source_segment_ids`
  - block roles/purpose/hints
- **Goal:** define generation plan and volume budget

Backend normalizes `target_chars` to match:

`duration_minutes * chars_per_minute`

## Stage 4: `block_writer` (loop)

- **Input per block:** one structure block + related source segments + previous block tail
- **Output:** plain text block
- **Goal:** write safely in chunks to avoid long-context collapse

Output is accumulated into `stages.block_writer.blocks[]`.

## Stage 5: `merger`

- **Input:** all written blocks + original source
- **Output:** single merged script
- **Goal:** remove seams and improve flow

## Stage 6: `quality_check`

- **Input:** original source + merger result + target char context
- **Output:** JSON quality assessment (`score/issues/approved` style)
- **Goal:** provide concrete fix guidance before final pass

## Stage 7: `final`

- **Input:** merger result + quality check result
- **Output:** final timed script
- **Goal:** polished delivery version with controlled length

Backend may force exact target length as last guardrail.

## Stage 8: `humanize_tts` (manual)

- **Input:** `final` result
- **Output:** expressive TTS-ready script for ElevenLabs
- **Modes:** `min`, `norm`, `max`
- **Goal:** punctuation + speech tags for vocal performance control

## Stage 9: `scene_builder` (manual)

- **Input:** `final` result
- **Output:** NDJSON scene stream:
  - `scene_id`
  - `text`
  - `start.prompt`
  - `end.prompt` (required)
  - `video.prompt`
- **Goal:** handoff to visual production pipeline

Scene Builder includes chunking and output cleanup to avoid dropped middle content.

## Why This Pipeline Exists

Single-call generation on long transcripts tends to:

- lose middle sections
- output placeholders
- drift in size and factual consistency

This staged design prioritizes controllability, observability, and deterministic recovery.

