"""Every setting in one place. Values come from .env (see .env.example)."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- model provider (any OpenAI-compatible API) ------------------------------------------
# openrouter: free models, ~50 requests/day without credit (https://openrouter.ai/keys)
# gemini:     Google AI Studio free key, no card, ~1,500 requests/day for Flash (https://aistudio.google.com/apikey)
# groq:       free key, no card, 1,000 requests/day but a small daily token limit (https://console.groq.com/keys)
# ollama:     a model running on your own computer, unlimited (https://ollama.com)
PROVIDERS = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", 0.0),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", 6.5),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", 2.1),
    "ollama": ("http://localhost:11434/v1", "", 0.0),
}
PROVIDER = os.getenv("AGENT_PROVIDER", "openrouter").strip().lower()
BASE_URL, KEY_ENV, _pace_default = PROVIDERS.get(PROVIDER, (os.getenv("LLM_BASE_URL", ""), "LLM_API_KEY", 0.0))
API_KEY = os.getenv(KEY_ENV, "") if KEY_ENV else ""
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# seconds between model calls, to stay under the provider's requests-per-minute limit
MIN_INTERVAL = float(os.getenv("MIN_SECONDS_BETWEEN_CALLS", _pace_default))
# Model ids differ per provider: list yours with  uv run plan.py models
MODEL = os.getenv("AGENT_MODEL", "google/gemma-4-31b-it:free")
FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "AGENT_FALLBACKS", "google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3-ultra-550b-a55b:free").split(",") if m.strip()]
# How much the model "thinks" before each step: low = much faster on Gemini 3 (empty = provider default)
REASONING_EFFORT = os.getenv("AGENT_REASONING_EFFORT", "low" if PROVIDER == "gemini" else "").strip().lower()
FAKE_LLM = os.getenv("PLANNER_FAKE_LLM", "0") == "1"   # offline scripted model for tests / demos without a key
USE_CACHE = os.getenv("PLANNER_CACHE", "1") == "1"      # replay identical requests from disk (free re-runs)

# --- harness budgets and limits ------------------------------------------------------------
MAX_STEPS = int(os.getenv("MAX_STEPS", "30"))          # model turns per run (≈ 2 + 3 per issue)
MAX_COST = float(os.getenv("MAX_COST", "0.10"))        # USD per run
MAX_RESULT_CHARS = 2500                                # tool results are cut to this size (context budget)
MAX_NOTES_CHARS = 6000                                 # meeting notes are cut to this size
MAX_SEARCH_RESULTS = 5

# --- GitLab -------------------------------------------------------------------------------
# Mock by default. Set all three to use a real GitLab project instead.
GITLAB_URL = os.getenv("GITLAB_URL", "")               # e.g. https://gitlab.com
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "")
GITLAB_PROJECT = os.getenv("GITLAB_PROJECT", "")       # numeric id or "group/project"
USE_REAL_GITLAB = bool(GITLAB_URL and GITLAB_TOKEN and GITLAB_PROJECT)

# "Today" decides which sprint is next. Fixed in evals so results don't change with the calendar.
TODAY = date.fromisoformat(os.getenv("PLANNER_TODAY", date.today().isoformat()))

# --- paths --------------------------------------------------------------------------------
DATA = ROOT / "data"
STATE = ROOT / "state"                       # runtime files (git-ignored)
TEAM_FILE = DATA / "team.json"
SEED_FILE = DATA / "gitlab_seed.json"
MOCK_DB = Path(os.getenv("GITLAB_MOCK_DB", STATE / "gitlab_mock.json"))
MEMORY_FILE = Path(os.getenv("MEMORY_FILE", STATE / "lead_memory.json"))
AUDIT_LOG = STATE / "audit.jsonl"
CACHE_DIR = STATE / "cache"
SPEC_FILE = ROOT / "AGENTS.md"
STATE.mkdir(exist_ok=True)
