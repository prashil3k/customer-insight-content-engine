import json
import time
import uuid
import config
from modules.company_brain import brief_as_prompt_context
from modules.insight_extractor import get_insights, mark_insight_used
from modules.pillar_map import get_gaps_as_prompt_context, get_pillar_names
from modules.model_manager import create_message


def _load_articles() -> dict:
    if config.ARTICLES_PATH.exists():
        return json.loads(config.ARTICLES_PATH.read_text())
    return {"articles": []}


def _save_articles(data: dict):
    config.ARTICLES_PATH.write_text(json.dumps(data, indent=2))


def get_articles(stage: str = None) -> list:
    data = _load_articles()
    articles = data["articles"]
    if stage:
        articles = [a for a in articles if a.get("stage") == stage]
    return sorted(articles, key=lambda a: a.get("updated_at", ""), reverse=True)


def get_article(article_id: str) -> dict | None:
    data = _load_articles()
    return next((a for a in data["articles"] if a["id"] == article_id), None)


def save_article(article: dict) -> dict:
    data = _load_articles()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    existing = next((i for i, a in enumerate(data["articles"]) if a["id"] == article["id"]), None)
    article["updated_at"] = now
    if existing is not None:
        data["articles"][existing] = article
    else:
        article["created_at"] = now
        data["articles"].append(article)
    _save_articles(data)
    return article


def generate_topics(num_topics: int = 5, progress_cb=None) -> list:
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    _p("Loading insights and pillar gaps...")
    insights = get_insights({"limit": 50, "min_confidence": 0.4})
    company_ctx = brief_as_prompt_context()
    pillar_ctx = get_gaps_as_prompt_context()
    pillar_names = get_pillar_names()
    existing_topics = [a["topic"] for a in get_articles()]

    # Build snippets with IDs so Claude can reference them back
    insight_id_map = {}  # short_key -> full insight id
    insight_snippets = []
    for i, ins in enumerate(insights[:20]):
        pain = ins["pain_points"][:2] if ins["pain_points"] else []
        quotes = [q["text"][:120] if isinstance(q, dict) else str(q)[:120] for q in ins["quotes"][:1]]
        angles = ins.get("content_angles", [])[:2]
        if pain or quotes or angles:
            short_key = f"INS_{i:02d}"
            insight_id_map[short_key] = ins["id"]
            source_label = ins.get("source_name") or ins["source_type"]
            sat = ins.get("saturation_score", 0)
            sat_note = " [SATURATED — this pain cluster is well-covered, deprioritise]" if sat >= 0.3 else ""
            insight_snippets.append(
                f"[ID:{short_key} | {ins['source_type']} / {ins.get('customer_segment', 'unknown')} | src:{source_label[:40]}{sat_note}] "
                f"Pains: {'; '.join(pain[:2])} | "
                f"Quote: {quotes[0] if quotes else 'none'} | "
                f"Angles: {'; '.join(angles)}"
            )

    insights_block = "\n".join(insight_snippets[:20]) if insight_snippets else "No insights yet."
    existing_block = "\n".join(f"- {t}" for t in existing_topics[:20]) if existing_topics else "None yet."

    _p("Generating topic proposals with Claude...")

    prompt = f"""{company_ctx}

{pillar_ctx}

You are an expert content strategist for Storylane. Generate {num_topics} high-impact content topic proposals based on the customer insights below.

AVAILABLE CONTENT PILLARS: {', '.join(pillar_names)}

INSIGHTS FROM CUSTOMER CALLS:
{insights_block}

TOPICS ALREADY IN PIPELINE (avoid duplicating):
{existing_block}

For each topic, generate a JSON object with:
- "topic": The article title (punchy, specific, not generic — reads like a real publication headline)
- "ideal_reader": Specific description — their role, company stage, current pain state. Not "B2B marketers". More like "VP of Sales at a 50-person SaaS company whose SEs are drowning in repetitive demos"
- "why_read": Honest answer to "why would a busy person read this?" — what do they get? (1-2 sentences, no BS)
- "angle": One of: thought_leadership, listicle, opinion, data_led, how_to, comparison, digital_pr
- "pillar": Which content pillar this belongs to (pick from the list above)
- "strategic_intent": Array from: eeat_signal, backlinkable, digital_pr, primary_voice, pillar_anchor
- "gap_it_fills": What coverage gap does this fill? (reference the pillar map gaps)
- "insight_ids_used": Array of short IDs (e.g. ["INS_00", "INS_03"]) for the insights that directly informed this topic proposal — reference the [ID:INS_XX] tags from the insights list above
- "social_signals": Array of 2-3 social media angles for the same insight, each with:
    - "format": carousel | post | short_video | poll
    - "hook": The opening line or angle for this social piece
    - "upgrade_to_article": true if this could actually be a full article instead

Return a JSON array of {num_topics} topic objects. No other text."""

    response = create_message("sonnet", max_tokens=4000, messages=[{"role": "user", "content": prompt}])

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()

    proposals = json.loads(raw)

    # Deduplication pass — check new proposals against existing pipeline articles
    overlap_map = {}  # proposal index -> {similar_to, reason}
    if existing_topics and proposals:
        _p("Checking for topic overlap with existing pipeline...")
        overlap_map = _check_topic_overlap(proposals, existing_topics)

    saved = []
    for i, p in enumerate(proposals):
        # Resolve short keys (INS_00, INS_03...) to real insight UUIDs
        raw_ids = p.get("insight_ids_used", [])
        resolved_ids = [insight_id_map[k] for k in raw_ids if k in insight_id_map]

        article = {
            "id": str(uuid.uuid4()),
            "topic": p.get("topic", ""),
            "ideal_reader": p.get("ideal_reader", ""),
            "why_read": p.get("why_read", ""),
            "angle": p.get("angle", "thought_leadership"),
            "pillar": p.get("pillar", ""),
            "strategic_intent": p.get("strategic_intent", []),
            "gap_it_fills": p.get("gap_it_fills", ""),
            "stage": "idea",
            "keywords": {},
            "draft": "",
            "qc_result": None,
            "seo_result": None,
            "social_signals": p.get("social_signals", []),
            "insight_ids_used": resolved_ids,
            "overlap_warning": overlap_map.get(i),  # None if no overlap
        }
        saved_article = save_article(article)
        saved.append(saved_article)

        # Mark contributing insights as used (so they show as "processed" in Insights tab)
        for ins_id in resolved_ids:
            try:
                mark_insight_used(ins_id, saved_article["id"])
            except Exception:
                pass

    _p(f"Generated {len(saved)} topic proposals.")
    return saved


def _check_topic_overlap(proposals: list, existing_topics: list) -> dict:
    """
    Ask Claude Haiku to flag semantic overlaps between new proposals and existing pipeline topics.
    Returns dict of { proposal_index: {"similar_to": "...", "reason": "..."} } for overlapping ones.
    """
    new_block = "\n".join(f"{i}. {p.get('topic','')} — {p.get('angle','')} — {p.get('gap_it_fills','')[:80]}" for i, p in enumerate(proposals))
    existing_block = "\n".join(f"- {t}" for t in existing_topics[:30])

    prompt = f"""You are checking for semantic overlap between new content topic proposals and an existing content pipeline.

NEW PROPOSALS:
{new_block}

EXISTING PIPELINE TOPICS:
{existing_block}

For each new proposal that substantially overlaps with an existing topic (same core argument, same angle, same primary question being answered — even if worded differently), return a warning.

Two topics overlap if a reader looking for one would likely be satisfied by the other. Different angles on the same question don't overlap. A how-to and an opinion piece on the same topic DO overlap.

Return a JSON object where keys are the proposal number (as a string, e.g. "0", "2") and values are objects with:
- "similar_to": the existing topic title it overlaps with
- "reason": one sentence explaining the overlap

Only include proposals that have meaningful overlap. If none overlap, return {{}}.
Return ONLY the JSON object."""

    try:
        response = create_message("haiku", max_tokens=800, messages=[{"role": "user", "content": prompt}])
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("```").strip()
        str_map = json.loads(raw)
        # Convert string keys to ints
        return {int(k): v for k, v in str_map.items()}
    except Exception:
        return {}  # dedup check is best-effort, never block topic generation


def create_article_manually(topic: str, angle: str, pillar: str = "") -> dict:
    article = {
        "id": str(uuid.uuid4()),
        "topic": topic,
        "ideal_reader": "",
        "why_read": "",
        "angle": angle,
        "pillar": pillar,
        "strategic_intent": [],
        "gap_it_fills": "",
        "stage": "idea",
        "keywords": {},
        "draft": "",
        "qc_result": None,
        "seo_result": None,
        "social_signals": [],
        "insight_ids_used": [],
    }
    return save_article(article)
