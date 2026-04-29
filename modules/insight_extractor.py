import json
import sqlite3
import time
import uuid
from pathlib import Path
import config
from modules.company_brain import brief_as_prompt_context
from modules.model_manager import create_message


def _init_db():
    conn = sqlite3.connect(str(config.INSIGHTS_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS insights (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_name TEXT,
            author TEXT,
            extracted_at TEXT,
            customer_segment TEXT,
            pain_points TEXT,
            quotes TEXT,
            use_cases TEXT,
            objections TEXT,
            competitors TEXT,
            metrics TEXT,
            tags TEXT,
            confidence REAL,
            used_in TEXT,
            raw_summary TEXT,
            raw_input TEXT
        )
    """)
    # Add raw_input column to existing DBs that predate this field
    try:
        conn.execute("ALTER TABLE insights ADD COLUMN raw_input TEXT")
        conn.commit()
    except Exception:
        pass  # column already exists
    conn.commit()
    conn.close()


def _load_sources() -> dict:
    if config.SOURCES_PATH.exists():
        return json.loads(config.SOURCES_PATH.read_text())
    return {}


def _save_sources(sources: dict):
    config.SOURCES_PATH.write_text(json.dumps(sources, indent=2))


def _source_already_processed(source_id: str) -> bool:
    return source_id in _load_sources()


def _mark_source_processed(source_id: str, meta: dict):
    sources = _load_sources()
    sources[source_id] = {**meta, "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _save_sources(sources)


def extract_insights_from_text(text: str, source_info: dict) -> dict | None:
    _init_db()
    source_id = source_info.get("source_id") or str(uuid.uuid4())

    if _source_already_processed(source_id):
        return None

    company_context = brief_as_prompt_context()

    prompt = f"""{company_context}

You are an expert product marketer for Storylane. Analyze the following content (call transcript, interview, or thought dump) and extract structured insights for content creation.

SOURCE TYPE: {source_info.get('source_type', 'unknown')}
SOURCE NAME: {source_info.get('source_name', 'unknown')}

CONTENT:
{text[:20000]}

Extract insights a content marketer would use. Return a JSON object with:
- "customer_segment": Who this person/company is (role, company type, stage) — be specific, not generic
- "pain_points": Array of specific pain points mentioned (concrete, not generic — use their actual words/framing)
- "quotes": Array of objects with "text" (verbatim or near-verbatim quote), "context" (what they were discussing), "content_value" (why this quote is powerful for content)
- "use_cases": Array of specific use cases or scenarios described
- "objections": Array of objections, hesitations, or concerns raised
- "competitors": Array of competitor names mentioned with any context
- "metrics": Array of specific numbers, stats, or measurable outcomes mentioned
- "content_angles": Array of 2-4 specific content ideas directly inspired by this conversation (be specific — not "write about demos" but "write about why X company replaced free trials with demos and saw Y result")
- "tags": Array of topic tags (e.g. "presales", "personalization", "mobile demos", "ROI")
- "confidence": Float 0-1 — how rich is this for content? (0.3 = thin, 0.7 = solid, 1.0 = gold)
- "raw_summary": 2-3 sentence summary of what this conversation was about

Return ONLY the JSON object."""

    response = create_message("sonnet", max_tokens=2000, messages=[{"role": "user", "content": prompt}])

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()

    data = json.loads(raw)

    insight_id = str(uuid.uuid4())
    conn = sqlite3.connect(str(config.INSIGHTS_DB_PATH))
    conn.execute(
        """INSERT INTO insights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            insight_id,
            source_id,
            source_info.get("source_type", "manual"),
            source_info.get("source_name", ""),
            source_info.get("author", ""),
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            data.get("customer_segment", ""),
            json.dumps(data.get("pain_points", [])),
            json.dumps(data.get("quotes", [])),
            json.dumps(data.get("use_cases", [])),
            json.dumps(data.get("objections", [])),
            json.dumps(data.get("competitors", [])),
            json.dumps(data.get("metrics", [])),
            json.dumps(data.get("tags", [])),
            data.get("confidence", 0.5),
            json.dumps([]),
            data.get("raw_summary", ""),
            text[:8000],  # store first 8k chars of original input
        ),
    )
    conn.commit()
    conn.close()

    _mark_source_processed(source_id, {"source_name": source_info.get("source_name"), "source_type": source_info.get("source_type")})
    return {**data, "id": insight_id, "source_id": source_id}


def add_thought_dump(text: str, author: str = "team") -> dict | None:
    source_id = f"dump_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    return extract_insights_from_text(text, {
        "source_id": source_id,
        "source_type": "thought_dump",
        "source_name": f"Thought dump by {author}",
        "author": author,
    })


def process_watch_folder(progress_cb=None) -> list:
    results = []
    for folder, source_type in [("grain", "grain"), ("sybill", "sybill")]:
        watch_path = config.WATCH_DIR / folder
        for f in watch_path.iterdir():
            if f.suffix not in (".txt", ".vtt", ".md", ".srt"):
                continue
            source_id = f"file_{f.name}_{f.stat().st_size}"
            if _source_already_processed(source_id):
                continue
            if progress_cb:
                progress_cb(f"Processing {f.name}")
            text = f.read_text(errors="replace")
            result = extract_insights_from_text(text, {
                "source_id": source_id,
                "source_type": source_type,
                "source_name": f.name,
            })
            if result:
                results.append(result)
    return results


def _compute_saturation(results: list) -> list:
    """
    Attach a saturation_score (0-1) to each insight.
    Score = fraction of other insights that share 2+ tags with this one.
    Above 0.3 (i.e. >30% of the corpus shares the same tag cluster) is considered saturated.
    """
    if len(results) < 2:
        for r in results:
            r["saturation_score"] = 0.0
        return results

    tag_sets = [set(r.get("tags", [])) for r in results]
    total = len(results) - 1  # exclude self

    for i, ins in enumerate(results):
        my_tags = tag_sets[i]
        if not my_tags:
            ins["saturation_score"] = 0.0
            continue
        overlap_count = sum(
            1 for j, other_tags in enumerate(tag_sets)
            if j != i and len(my_tags.intersection(other_tags)) >= 2
        )
        ins["saturation_score"] = round(overlap_count / total, 2)

    return results


def get_insights(filters: dict = None) -> list:
    _init_db()
    filters = filters or {}
    conn = sqlite3.connect(str(config.INSIGHTS_DB_PATH))
    conn.row_factory = sqlite3.Row
    query = "SELECT * FROM insights"
    params = []
    conditions = []
    if filters.get("source_type"):
        conditions.append("source_type = ?")
        params.append(filters["source_type"])
    if filters.get("tag"):
        conditions.append("tags LIKE ?")
        params.append(f'%"{filters["tag"]}"%')
    if filters.get("min_confidence"):
        conditions.append("confidence >= ?")
        params.append(float(filters["min_confidence"]))
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY extracted_at DESC"
    if filters.get("limit"):
        query += f" LIMIT {int(filters['limit'])}"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    results = []
    for row in rows:
        d = dict(row)
        for field in ["pain_points", "quotes", "use_cases", "objections", "competitors", "metrics", "tags", "used_in"]:
            try:
                d[field] = json.loads(d[field] or "[]")
            except Exception:
                d[field] = []
        results.append(d)

    return _compute_saturation(results)


def get_insights_by_ids(ids: list) -> list:
    """Fetch specific insights by their UUIDs."""
    _init_db()
    if not ids:
        return []
    conn = sqlite3.connect(str(config.INSIGHTS_DB_PATH))
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM insights WHERE id IN ({placeholders})", ids).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        for field in ["pain_points", "quotes", "use_cases", "objections", "competitors", "metrics", "tags", "used_in"]:
            try:
                d[field] = json.loads(d[field] or "[]")
            except Exception:
                d[field] = []
        results.append(d)
    return results


def mark_insight_used(insight_id: str, article_id: str):
    _init_db()
    conn = sqlite3.connect(str(config.INSIGHTS_DB_PATH))
    row = conn.execute("SELECT used_in FROM insights WHERE id = ?", (insight_id,)).fetchone()
    if row:
        used = json.loads(row[0] or "[]")
        if article_id not in used:
            used.append(article_id)
        conn.execute("UPDATE insights SET used_in = ? WHERE id = ?", (json.dumps(used), insight_id))
        conn.commit()
    conn.close()


def get_insight_stats() -> dict:
    _init_db()
    conn = sqlite3.connect(str(config.INSIGHTS_DB_PATH))
    total = conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0]
    by_type = conn.execute("SELECT source_type, COUNT(*) FROM insights GROUP BY source_type").fetchall()
    conn.close()
    return {"total": total, "by_type": {r[0]: r[1] for r in by_type}}
