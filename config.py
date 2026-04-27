import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
DEFAULT_MODEL = "gpt-4.1"

STAGE_TEMPERATURES = {
    "pre_analysis":   0.4,
    "analysis":       0.3,
    "structure":      0.5,
    "block_writer":   0.8,
    "merger":         0.4,
    "quality_check":  0.3,
    "final":          0.3,
    "humanize_tts":   0.5,
}

os.makedirs(PROJECTS_DIR, exist_ok=True)
