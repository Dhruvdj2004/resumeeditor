import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "resumes.db"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = ROOT / "static"

# Providers are tried in this order; any without an API key are skipped.
PROVIDER_ORDER = [
    p.strip().lower()
    for p in os.getenv("PROVIDER_ORDER", "groq,cerebras,gemini,openrouter").split(",")
    if p.strip()
]
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "").strip()
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free,google/gemma-4-31b-it:free,qwen/qwen3.8-27b:free")

COMPILE_TIMEOUT = int(os.getenv("COMPILE_TIMEOUT", "120"))
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_EDIT_RETRIES = 2
