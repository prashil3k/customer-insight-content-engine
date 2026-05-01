import json
import time
import requests
import config
from modules.company_brain import brief_as_prompt_context
from modules.model_manager import create_message


def _load_cache() -> dict:
    if config.KW_CACHE_PATH.exists():
        return json.loads(config.KW_CACHE_PATH.read_text())
    return {}


def _save_cache(cache: dict):
    config.KW_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def _get_kw_settings() -> dict:
    """Load keyword research settings with defaults."""
    settings = config.load_settings()
    kw_cfg = settings.get("keyword_settings", {})
    return {
        "country": kw_cfg.get("country", "us"),
        "max_kd": kw_cfg.get("max_kd", 70),
        "min_volume": kw_cfg.get("min_volume", 0),
        "skip_ahrefs": kw_cfg.get("skip_ahrefs", False),
    }


def _ahrefs_headers():
    return {
        "Authorization": f"Bearer {config.AHREFS_API_TOKEN}",
        "Accept": "application/json",
    }


def _ahrefs_kw_data(keywords: list, country: str = "us") -> dict:
    if not config.AHREFS_API_TOKEN:
        return {}

    cache = _load_cache()
    cache_key_suffix = f"_{country}" if country != "us" else ""
    results = {}
    to_fetch = []

    for kw in keywords:
        ck = kw.lower().strip() + cache_key_suffix
        if ck in cache and cache[ck].get("cached_at"):
            results[kw.lower().strip()] = cache[ck]
        else:
            to_fetch.append(kw.lower().strip())

    for kw in to_fetch:
        try:
            params = {
                "select": "keyword,volume,difficulty,cpc,clicks",
                "keywords": kw,
                "country": country,
            }
            r = requests.get(
                f"{config.AHREFS_BASE}/keywords-explorer/overview",
                headers=_ahrefs_headers(),
                params=params,
                timeout=20,
            )
            if r.status_code == 429:
                time.sleep(60)
                r = requests.get(f"{config.AHREFS_BASE}/keywords-explorer/overview",
                                  headers=_ahrefs_headers(), params=params, timeout=20)

            if r.status_code == 200:
                data = r.json()
                kw_data = {}
                if "keywords" in data and data["keywords"]:
                    kw_data = data["keywords"][0]
                elif isinstance(data, dict):
                    kw_data = data

                entry = {
                    "keyword": kw,
                    "volume": kw_data.get("volume", 0),
                    "difficulty": kw_data.get("difficulty", kw_data.get("kd", 0)),
                    "cpc": kw_data.get("cpc", 0),
                    "cached_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            else:
                entry = {"keyword": kw, "volume": None, "difficulty": None, "error": r.status_code}

            results[kw] = entry
            cache[kw + cache_key_suffix] = entry
            time.sleep(0.5)
        except Exception as e:
            results[kw] = {"keyword": kw, "volume": None, "difficulty": None, "error": str(e)}

    _save_cache(cache)
    return results


def _ahrefs_serp_overview(topic: str, country: str = "us") -> dict:
    """
    Fetch SERP overview for a topic from Ahrefs.
    Returns dict with top_results, avg_word_count, featured_snippet, dominant_format.
    Falls back gracefully if unavailable.
    """
    if not config.AHREFS_API_TOKEN:
        return {}

    try:
        params = {
            "keyword": topic,
            "country": country,
            "select": "url,title,word_count,type,rank",
            "limit": 10,
        }
        r = requests.get(
            f"{config.AHREFS_BASE}/serp-overview",
            headers=_ahrefs_headers(),
            params=params,
            timeout=20,
        )
        if r.status_code != 200:
            return {"error": r.status_code, "raw": r.text[:200]}

        data = r.json()
        results = data.get("serp", data.get("results", []))
        if not results:
            return {}

        # Derive useful signals
        word_counts = [r.get("word_count", 0) for r in results if r.get("word_count")]
        avg_wc = round(sum(word_counts) / len(word_counts)) if word_counts else 0

        types = [r.get("type", "") for r in results]
        type_counts = {}
        for t in types:
            if t:
                type_counts[t] = type_counts.get(t, 0) + 1
        dominant_format = max(type_counts, key=type_counts.get) if type_counts else "unknown"

        has_featured_snippet = any(r.get("type") == "featured_snippet" for r in results)

        return {
            "top_results": [{"title": r.get("title", ""), "url": r.get("url", ""), "word_count": r.get("word_count", 0)} for r in results[:5]],
            "avg_word_count": avg_wc,
            "dominant_format": dominant_format,
            "featured_snippet": has_featured_snippet,
            "total_results_checked": len(results),
        }
    except Exception as e:
        return {"error": str(e)}


def _brainstorm_keywords(topic: str, ideal_reader: str, angle: str,
                         serp_context: str = "", manual_keywords: list = None) -> list:
    company_ctx = brief_as_prompt_context()
    serp_block = f"\nSERP CONTEXT (what's currently ranking for this topic):\n{serp_context}\n" if serp_context else ""
    manual_block = (
        "\nUSER-SPECIFIED KEYWORDS (include these verbatim in output):\n" +
        "\n".join(f"- {k}" for k in manual_keywords) + "\n"
    ) if manual_keywords else ""

    prompt = f"""{company_ctx}{serp_block}{manual_block}
Topic: {topic}
Ideal reader: {ideal_reader}
Content angle: {angle}

This topic is niche — it comes directly from B2B customer conversations. Your job is NOT to brainstorm synonyms of the topic title. Your job is to find the highest-volume NEARBY keywords this article can credibly rank for.

Think in layers:
1. PARENT CATEGORY — What broader problem or category does this topic sit inside? What does the ideal reader search for before they know about this specific angle? (e.g. if topic is "how presales teams cut demo prep time", the parent could be "sales demo best practices", "presales productivity", "demo automation software")
2. PROBLEM-AWARE — What does someone type when they have the pain but haven't found a solution?
3. SOLUTION-AWARE — What do they search when actively evaluating tools or approaches?
4. BOFU — comparison, "best X for Y", "X alternative" searches where relevant

Generate 15 keyword candidates spanning these layers. Skew toward broader parent-category terms with more likely search volume — the article covers the specific topic but can rank for broader searches it genuinely answers.
{f"These keywords were specified by the user — include them verbatim: {', '.join(manual_keywords)}" if manual_keywords else ""}
{f"Use the SERP context above to inform which keyword formats are getting traction." if serp_context else ""}

Return a JSON array of keyword strings only. No explanations."""

    response = create_message("haiku", max_tokens=800, messages=[{"role": "user", "content": prompt}])
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()
    candidates = json.loads(raw)

    # Guarantee manual keywords are included even if Claude dropped them
    if manual_keywords:
        existing_lower = {c.lower().strip() for c in candidates}
        for mk in manual_keywords:
            if mk.lower().strip() not in existing_lower:
                candidates.append(mk)

    return candidates


def research_keywords(topic: str, ideal_reader: str = "", angle: str = "",
                      manual_keywords: list = None, progress_cb=None) -> dict:
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    kw_cfg = _get_kw_settings()
    country = kw_cfg["country"]
    max_kd = kw_cfg["max_kd"]
    min_volume = kw_cfg["min_volume"]
    skip_ahrefs = kw_cfg["skip_ahrefs"] or not config.AHREFS_API_TOKEN

    # ── Pass 1: SERP overview ────────────────────────────────────────────────
    serp_data = {}
    serp_context = ""
    if not skip_ahrefs:
        _p(f"Fetching SERP overview for '{topic[:50]}'...")
        serp_data = _ahrefs_serp_overview(topic, country)
        if serp_data and not serp_data.get("error"):
            lines = []
            if serp_data.get("dominant_format"):
                lines.append(f"Dominant format: {serp_data['dominant_format']}")
            if serp_data.get("avg_word_count"):
                lines.append(f"Average word count in top results: {serp_data['avg_word_count']}")
            if serp_data.get("featured_snippet"):
                lines.append("Featured snippet present: yes")
            if serp_data.get("top_results"):
                lines.append("Top ranking titles: " + " | ".join(r["title"][:60] for r in serp_data["top_results"][:3]))
            serp_context = "\n".join(lines)
            _p(f"SERP: {serp_data.get('dominant_format','?')} format · avg {serp_data.get('avg_word_count','?')} words · snippet: {serp_data.get('featured_snippet', False)}")
        else:
            _p("SERP overview unavailable — continuing without it.")

    # ── Pass 2: Keyword brainstorm (SERP-informed, manual-seeded) ───────────────
    _p("Brainstorming keyword candidates...")
    candidates = _brainstorm_keywords(topic, ideal_reader, angle, serp_context, manual_keywords)

    # ── Pass 3: Ahrefs validation ────────────────────────────────────────────
    if not skip_ahrefs:
        _p(f"Validating {len(candidates)} candidates with Ahrefs ({country.upper()})...")
    else:
        _p("Ahrefs skipped — using brainstorm scores only.")

    manual_set = {k.lower().strip() for k in (manual_keywords or [])}
    ahrefs_data = _ahrefs_kw_data(candidates[:18], country) if not skip_ahrefs else {}

    scored = []
    for kw in candidates:
        kw_lower = kw.lower().strip()
        data = ahrefs_data.get(kw_lower, {})
        vol = data.get("volume") or 0
        kd = data.get("difficulty") or 50
        is_manual = kw_lower in manual_set

        if vol and kd:
            score = (vol / 1000) * max(0, (70 - kd) / 70)
        else:
            score = 0.1

        # Manual keywords always float to the top regardless of score
        if is_manual:
            score += 1000

        scored.append({
            "keyword": kw,
            "volume": vol if vol else "unknown",
            "difficulty": kd if kd else "unknown",
            "cpc": data.get("cpc", 0),
            "score": round(score, 3),
            "source": "manual" if is_manual else ("ahrefs" if data.get("cached_at") else "brainstorm"),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # Apply filters — never filter out manual keywords
    if max_kd < 100:
        filtered = [k for k in scored if k["source"] == "manual" or k["difficulty"] == "unknown" or k["difficulty"] <= max_kd]
        if filtered:
            scored = filtered
    if min_volume > 0:
        filtered = [k for k in scored if k["source"] == "manual" or k["volume"] == "unknown" or k["volume"] >= min_volume]
        if filtered:
            scored = filtered

    primary = scored[0] if scored else {"keyword": topic, "volume": "unknown", "difficulty": "unknown", "source": "brainstorm"}
    secondary = scored[1:8]  # full objects with volume/KD (expanded to 7 to show more options)

    _p("Keyword research complete.")
    return {
        "primary": primary,
        "secondary": secondary,
        "all_candidates": scored,
        "serp": serp_data if serp_data and not serp_data.get("error") else None,
        "settings_used": {"country": country, "max_kd": max_kd, "min_volume": min_volume},
        "researched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def check_topics_demand(articles: list, progress_cb=None) -> None:
    """
    Batch Ahrefs volume+KD check for a list of idea-stage articles.
    Writes demand_signal onto each article and saves. No-ops if Ahrefs unavailable.
    """
    if not config.AHREFS_API_TOKEN or not articles:
        return

    from modules.topic_planner import save_article

    topics = [a["topic"] for a in articles if a.get("topic")]
    if not topics:
        return

    if progress_cb:
        progress_cb(f"Checking search demand for {len(topics)} topics...")

    kw_data = _ahrefs_kw_data(topics)

    for a in articles:
        topic_key = a.get("topic", "").lower().strip()
        data = kw_data.get(topic_key) or kw_data.get(a.get("topic", ""))
        if data and data.get("volume") is not None:
            a["demand_signal"] = {
                "volume": data["volume"],
                "difficulty": data["difficulty"],
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            save_article(a)
