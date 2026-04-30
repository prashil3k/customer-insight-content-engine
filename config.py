import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
WATCH_DIR = BASE_DIR / "watch"
OUTPUT_DIR = BASE_DIR / "output" / "articles"
STATIC_DIR = BASE_DIR / "static"

DEMO_CLASSIFIER_DIR = BASE_DIR / "storylane-demo-classifier"
DEMO_INDEX_PATH = DEMO_CLASSIFIER_DIR / "demo_index.json"
DEMO_SCREENSHOTS_DIR = DEMO_CLASSIFIER_DIR / "screenshots"

for _d in [DATA_DIR, WATCH_DIR / "grain", WATCH_DIR / "sybill", OUTPUT_DIR, STATIC_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

def _load_saved_keys() -> dict:
    settings_path = BASE_DIR / "data" / "settings.json"
    if settings_path.exists():
        try:
            d = json.loads(settings_path.read_text())
            return d.get("api_keys", {})
        except Exception:
            pass
    return {}

_saved_keys = _load_saved_keys()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or _saved_keys.get("anthropic", "")
AHREFS_API_TOKEN = os.environ.get("AHREFS_API_TOKEN") or _saved_keys.get("ahrefs", "")
GRAIN_API_TOKEN = os.environ.get("GRAIN_API_TOKEN") or _saved_keys.get("grain", "")
SYBILL_API_TOKEN = os.environ.get("SYBILL_API_TOKEN") or _saved_keys.get("sybill", "")


def reload_keys():
    """Call after saving new keys so modules pick them up without restart."""
    import sys
    global ANTHROPIC_API_KEY, AHREFS_API_TOKEN, GRAIN_API_TOKEN, SYBILL_API_TOKEN
    saved = _load_saved_keys()
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or saved.get("anthropic", "")
    AHREFS_API_TOKEN = os.environ.get("AHREFS_API_TOKEN") or saved.get("ahrefs", "")
    GRAIN_API_TOKEN = os.environ.get("GRAIN_API_TOKEN") or saved.get("grain", "")
    SYBILL_API_TOKEN = os.environ.get("SYBILL_API_TOKEN") or saved.get("sybill", "")
    for mod_name in ["modules.company_brain", "modules.insight_extractor",
                     "modules.topic_planner", "modules.keyword_researcher",
                     "modules.draft_generator", "modules.qc_engine",
                     "modules.seo_engine", "modules.template_learner"]:
        mod = sys.modules.get(mod_name)
        if mod and hasattr(mod, "_client"):
            mod._client = None


def load_settings() -> dict:
    """Load full settings.json, merged with defaults."""
    if SETTINGS_PATH.exists():
        try:
            d = json.loads(SETTINGS_PATH.read_text())
            return {**DEFAULT_SETTINGS, **d}
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()


SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5-20251001"

PORT = 8001
HOST = "0.0.0.0"

COMPANY_BRIEF_PATH = DATA_DIR / "company_brief.json"
INSIGHTS_DB_PATH = DATA_DIR / "insights.db"
SOURCES_PATH = DATA_DIR / "sources.json"
ARTICLES_PATH = DATA_DIR / "articles.json"
PILLARS_PATH = DATA_DIR / "content_pillars.json"
FORMATS_PATH = DATA_DIR / "content_formats.json"
KW_CACHE_PATH = DATA_DIR / "kw_cache.json"
SETTINGS_PATH = DATA_DIR / "settings.json"

AHREFS_BASE = "https://api.ahrefs.com/v3"

DEMO_CLASSIFIER_URL = os.environ.get("DEMO_CLASSIFIER_URL", "http://localhost:8000")
DEMO_QUERY_CACHE_PATH = DATA_DIR / "demo_query_cache.json"

DEFAULT_PILLARS = [
    "Demo Automation & Self-Service",
    "PreSales Efficiency",
    "Demo Personalization at Scale",
    "Buyer Enablement & Deal Rooms",
    "Demo Analytics & ROI",
    "AI in the Demo Process",
    "Sales Enablement & Rep Adoption",
    "Security & Compliance in Demos",
    "Onboarding & Customer Training",
    "Event & Offline Demo Strategy",
]

DEFAULT_SETTINGS = {
    "scheduler": {
        "insight_scan_hours": 6,
        "topic_gen_hours": 24,
        "enabled": True,
        "auto_topics_after_scan": False,  # generate topics immediately after insight scan
    },
    "auto_apply": {
        "qc": False,
        "seo": False,
    },
    "auto_advance": False,
    "auto_validate": True,   # run decoupled validation after applying suggestions
    "topic_gen_count": 5,
    "qc_rubric": "",
    "seo_rubric": "",
    "keyword_settings": {
        "country": "us",
        "max_kd": 70,
        "min_volume": 0,
        "skip_ahrefs": False,
    },
}
