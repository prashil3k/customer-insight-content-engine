import json
import time
import config

CONTENT_TYPES = ["thought_leadership", "listicle", "how_to", "comparison", "data_led", "opinion", "digital_pr"]
EEAT_SIGNALS = ["primary_voice", "original_data", "expert_opinion", "customer_proof"]
STRATEGIC_INTENTS = ["eeat_signal", "backlinkable", "digital_pr", "primary_voice", "pillar_anchor"]


def _load() -> dict:
    if config.PILLARS_PATH.exists():
        return json.loads(config.PILLARS_PATH.read_text())
    return _default_map()


def _save(data: dict):
    config.PILLARS_PATH.write_text(json.dumps(data, indent=2))


def _default_map() -> dict:
    pillars = {}
    for name in config.DEFAULT_PILLARS:
        pillars[name] = {
            "name": name,
            "description": "",
            "coverage": {ct: [] for ct in CONTENT_TYPES},
            "eeat_coverage": {sig: [] for sig in EEAT_SIGNALS},
            "article_ids": [],
        }
    return {"pillars": pillars, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def get_pillar_map() -> dict:
    return _load()


def get_pillar_names() -> list:
    return list(_load()["pillars"].keys())


def add_pillar(name: str, description: str = ""):
    data = _load()
    if name not in data["pillars"]:
        data["pillars"][name] = {
            "name": name,
            "description": description,
            "coverage": {ct: [] for ct in CONTENT_TYPES},
            "eeat_coverage": {sig: [] for sig in EEAT_SIGNALS},
            "article_ids": [],
        }
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save(data)


def update_pillar_coverage(article_id: str, pillar: str, content_type: str, eeat_signals: list = None):
    data = _load()
    if pillar not in data["pillars"]:
        add_pillar(pillar)
        data = _load()
    p = data["pillars"][pillar]
    if content_type in p["coverage"] and article_id not in p["coverage"][content_type]:
        p["coverage"][content_type].append(article_id)
    if article_id not in p["article_ids"]:
        p["article_ids"].append(article_id)
    for sig in (eeat_signals or []):
        if sig in p["eeat_coverage"] and article_id not in p["eeat_coverage"][sig]:
            p["eeat_coverage"][sig].append(article_id)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save(data)


def get_pillar_gaps() -> list:
    data = _load()
    gaps = []
    for pillar_name, pillar in data["pillars"].items():
        missing_types = [ct for ct in CONTENT_TYPES if not pillar["coverage"].get(ct)]
        thin_eeat = [sig for sig in EEAT_SIGNALS if not pillar["eeat_coverage"].get(sig)]
        total_articles = len(pillar.get("article_ids", []))
        if missing_types or thin_eeat:
            gaps.append({
                "pillar": pillar_name,
                "total_articles": total_articles,
                "missing_content_types": missing_types,
                "thin_eeat_signals": thin_eeat,
                "priority": "high" if total_articles == 0 else ("medium" if len(missing_types) > 3 else "low"),
            })
    return sorted(gaps, key=lambda g: (g["total_articles"], len(g["missing_content_types"])))


def get_gaps_as_prompt_context() -> str:
    gaps = get_pillar_gaps()
    if not gaps:
        return "All content pillars have good coverage."
    lines = ["=== CONTENT PILLAR GAPS (highest priority first) ==="]
    for g in gaps[:5]:
        lines.append(
            f"- {g['pillar']}: missing {', '.join(g['missing_content_types'][:3])} "
            f"| thin EEAT: {', '.join(g['thin_eeat_signals'][:2])} "
            f"| {g['total_articles']} article(s) so far [{g['priority']} priority]"
        )
    lines.append("=== END PILLAR GAPS ===")
    return "\n".join(lines)
