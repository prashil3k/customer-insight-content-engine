import json
import time
import requests
from pathlib import Path
import config

_index_cache = None
_index_mtime = None

_query_cache = None


def _load_index() -> dict:
    global _index_cache, _index_mtime
    if not config.DEMO_INDEX_PATH.exists():
        return {"demos": {}}
    mtime = config.DEMO_INDEX_PATH.stat().st_mtime
    if _index_cache is None or mtime != _index_mtime:
        _index_cache = json.loads(config.DEMO_INDEX_PATH.read_text())
        _index_mtime = mtime
    return _index_cache


def _load_query_cache() -> dict:
    global _query_cache
    if _query_cache is None:
        if config.DEMO_QUERY_CACHE_PATH.exists():
            try:
                _query_cache = json.loads(config.DEMO_QUERY_CACHE_PATH.read_text())
            except Exception:
                _query_cache = {}
        else:
            _query_cache = {}
    return _query_cache


def _save_query_cache(cache: dict):
    global _query_cache
    _query_cache = cache
    config.DEMO_QUERY_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def _normalize_query(query: str) -> str:
    return query.lower().strip()


def _query_classifier(topic: str) -> list | None:
    """Call /query-engine on the demo classifier. Returns list of demo dicts or None on failure."""
    try:
        r = requests.post(
            f"{config.DEMO_CLASSIFIER_URL}/query-engine",
            json={"query": topic},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("results") or data.get("demos") or []
    except Exception:
        return None


def _score_demo_for_topic(demo: dict, topic_lower: str, angle: str) -> float:
    score = 0.0
    classification = demo.get("classification") or {}
    overall = classification.get("overall_score", 0)
    score += overall * 0.5

    name = (demo.get("name") or "").lower()
    summary = (classification.get("summary") or "").lower()
    steps_text = " ".join(
        s.get("text", "") for s in (demo.get("steps_text") or [])
    ).lower()[:2000]

    # Boost from classifier-generated insights
    insights_text = " ".join(demo.get("insights") or []).lower()
    layers_text = " ".join(demo.get("layers") or []).lower()

    # Boost from q: tags
    tags = demo.get("tags") or []
    topic_words = set(topic_lower.split())

    for word in topic_words:
        if len(word) > 4:
            if word in name:
                score += 1.5
            if word in summary:
                score += 0.5
            if word in steps_text:
                score += 0.3
            if word in insights_text:
                score += 0.8
            if word in layers_text:
                score += 0.5

    # q:theme tag boost
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("q:"):
            theme = tag[2:].lower()
            if any(w in theme for w in topic_words if len(w) > 3):
                score += 1.2

    if demo.get("is_accessible") and not demo.get("is_gated"):
        score += 1.0

    return score


def find_best_demos(topic: str, angle: str = "", num: int = 3) -> list:
    """Find best demos for a topic. Tries classifier API first, falls back to local index."""
    key = _normalize_query(topic)
    cache = _load_query_cache()

    # Check cache (1 hour TTL)
    if key in cache:
        entry = cache[key]
        if time.time() - entry.get("ts", 0) < 3600:
            return entry["results"][:num]

    # Try classifier API
    api_results = _query_classifier(topic)
    if api_results is not None:
        results = []
        for demo in api_results[:num]:
            classification = demo.get("classification") or {}
            results.append({
                "name": demo.get("name", ""),
                "demo_url": demo.get("demo_url", ""),
                "showcase_url": demo.get("showcase_url", ""),
                "overall_score": classification.get("overall_score", 0),
                "type": classification.get("type", ""),
                "summary": (classification.get("summary") or "")[:200],
                "relevance_score": demo.get("relevance_score", 0),
                "has_screenshot": screenshot_exists(demo.get("name", ""), 1),
                "screenshot_path": get_screenshot_path(demo.get("name", ""), 1),
                "steps_count": demo.get("total_steps", 0),
                "insights": demo.get("insights") or [],
                "source": "classifier",
            })
        cache[key] = {"ts": time.time(), "results": results}
        _save_query_cache(cache)
        return results[:num]

    # Fallback: local index scoring
    return _find_best_demos_local(topic, angle, num)


def _find_best_demos_local(topic: str, angle: str = "", num: int = 3) -> list:
    index = _load_index()
    demos = index.get("demos", {})
    topic_lower = topic.lower()

    scored = []
    for key, demo in demos.items():
        classification = demo.get("classification") or {}
        if classification.get("overall_score", 0) < 3:
            continue
        if demo.get("is_gated"):
            continue
        s = _score_demo_for_topic(demo, topic_lower, angle)
        scored.append((s, demo))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, demo in scored[:num]:
        classification = demo.get("classification") or {}
        results.append({
            "name": demo.get("name", ""),
            "demo_url": demo.get("demo_url", ""),
            "showcase_url": demo.get("showcase_url", ""),
            "overall_score": classification.get("overall_score", 0),
            "type": classification.get("type", ""),
            "summary": (classification.get("summary") or "")[:200],
            "relevance_score": round(score, 2),
            "has_screenshot": screenshot_exists(demo.get("name", ""), 1),
            "screenshot_path": get_screenshot_path(demo.get("name", ""), 1),
            "steps_count": demo.get("total_steps", 0),
            "insights": demo.get("insights") or [],
            "source": "local",
        })
    return results


def deep_scan_for_topic(topic: str) -> dict:
    """Trigger /run-scan on the classifier with topic as query_context. Returns scan result or error."""
    try:
        r = requests.post(
            f"{config.DEMO_CLASSIFIER_URL}/run-scan",
            json={"query_context": topic},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        return {"error": "Demo classifier not running", "demos_scanned": 0}
    except Exception as e:
        return {"error": str(e), "demos_scanned": 0}


def get_knowledge_status() -> dict:
    """Get knowledge base status from classifier."""
    try:
        r = requests.get(f"{config.DEMO_CLASSIFIER_URL}/knowledge", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {"status": "offline", "total_queries": 0, "total_demos_with_insights": 0}


def screenshot_exists(company_name: str, step: int = 1) -> bool:
    path = get_screenshot_path(company_name, step)
    return path is not None and Path(path).exists()


def get_screenshot_path(company_name: str, step: int = 1) -> str | None:
    if not company_name:
        return None
    step_str = f"step_{step:03d}.png"
    candidate = config.DEMO_SCREENSHOTS_DIR / company_name / step_str
    if candidate.exists():
        return str(candidate)
    for folder in config.DEMO_SCREENSHOTS_DIR.iterdir():
        if folder.is_dir() and folder.name.lower() == company_name.lower():
            p = folder / step_str
            if p.exists():
                return str(p)
    return None


def search_demos(query: str, limit: int = 10) -> list:
    index = _load_index()
    demos = index.get("demos", {})
    query_lower = query.lower()
    results = []
    for key, demo in demos.items():
        name = (demo.get("name") or "").lower()
        category = (demo.get("category") or "").lower()
        classification = demo.get("classification") or {}
        summary = (classification.get("summary") or "").lower()
        if query_lower in name or query_lower in category or query_lower in summary:
            results.append({
                "name": demo.get("name", ""),
                "demo_url": demo.get("demo_url", ""),
                "overall_score": classification.get("overall_score", 0),
                "type": classification.get("type", ""),
            })
    results.sort(key=lambda x: x["overall_score"], reverse=True)
    return results[:limit]


def get_demo_stats() -> dict:
    index = _load_index()
    demos = index.get("demos", {})
    classified = [d for d in demos.values() if d.get("classification")]
    scores = [d["classification"].get("overall_score", 0) for d in classified if d.get("classification")]
    return {
        "total": len(demos),
        "classified": len(classified),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "last_sync": index.get("last_sync", "never"),
    }
