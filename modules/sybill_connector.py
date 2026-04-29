"""
Sybill REST API poller.

Pulls conversations since last run using GET /v1/conversations,
fetches full detail (transcript + AI summary) for each new external call,
and feeds into extract_insights_from_text().

Authentication: Bearer sk_live_... key from Settings → API Keys → Sybill.
No webhook or ngrok required — this is a pure pull integration.
"""

import json
import time
import requests
from datetime import datetime, timezone, timedelta

import config
from modules.insight_extractor import extract_insights_from_text

SYBILL_API_BASE = "https://api.sybill.ai"

DEFAULT_SYBILL_FILTER = {
    "exclude_title_patterns": [
        "scrum", "standup", "stand-up", "1:1", "one on one",
        "all hands", "all-hands", "offsite", "interview",
    ],
    "require_keywords": [
        "demo", "onboarding", "pricing", "objection", "competitor",
        "pain point", "use case", "buyer", "trial", "evaluation",
    ],
    "min_duration_minutes": 10,
    "min_transcript_words": 200,
    "lookback_days": 90,
}


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.SYBILL_API_TOKEN}"}


def _load_sources() -> dict:
    if config.SOURCES_PATH.exists():
        try:
            return json.loads(config.SOURCES_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_sources(sources: dict):
    config.SOURCES_PATH.write_text(json.dumps(sources, indent=2))


def _get_filter_config() -> dict:
    settings = config.load_settings()
    return {**DEFAULT_SYBILL_FILTER, **settings.get("sybill_filter", {})}


def _duration_minutes(conv: dict) -> float:
    try:
        start = datetime.fromisoformat(conv["startTime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(conv["endTime"].replace("Z", "+00:00"))
        return (end - start).total_seconds() / 60
    except Exception:
        return 0


def _is_relevant_call(conv: dict, filt: dict) -> bool:
    """Filter to external customer calls worth processing."""
    # Only external meetings
    if conv.get("type") != "EXTERNAL":
        return False

    # Title exclusion patterns
    title = conv.get("title", "").lower()
    for pattern in filt.get("exclude_title_patterns", []):
        if pattern.lower() in title:
            return False

    # Duration filter
    min_dur = filt.get("min_duration_minutes", 0)
    if min_dur and _duration_minutes(conv) < min_dur:
        return False

    return True


def _build_transcript_text(conv_id: str, detail: dict) -> str:
    """Build a readable text block from a conversation detail response."""
    title = detail.get("title", "Sybill call")
    start = detail.get("startTime", "")[:10]
    participants = detail.get("participants", [])
    transcript_entries = detail.get("transcript", [])
    summary = detail.get("summary", {})

    external = [p["name"] for p in participants if p.get("name") and
                not any(d in p.get("email", "") for d in ["storylane.io"])]
    internal = [p["name"] for p in participants if p.get("name") and
                "storylane.io" in p.get("email", "")]

    lines = [f"Call: {title}", f"Date: {start}", f"ID: {conv_id}"]
    if external:
        lines.append(f"Customer: {', '.join(external)}")
    if internal:
        lines.append(f"Storylane: {', '.join(internal)}")
    lines.append("")

    if transcript_entries:
        lines.append("TRANSCRIPT:")
        current_speaker = None
        for entry in transcript_entries:
            speaker = entry.get("speaker", "Unknown")
            text = entry.get("text", "").strip()
            if not text:
                continue
            if speaker != current_speaker:
                lines.append(f"\n{speaker}:")
                current_speaker = speaker
            lines.append(text)
    elif summary:
        # Fall back to AI summary if no transcript
        lines.append("AI SUMMARY:")
        outcome = summary.get("Outcome", "")
        if outcome:
            lines.append(f"Outcome: {outcome}")
        takeaways = summary.get("Key Takeaways", [])
        if takeaways:
            lines.append("\nKey Takeaways:")
            for t in takeaways:
                lines.append(f"- {t.get('topic', '')}: {t.get('key_takeaway', '')}")
        pain_points = summary.get("Pain Points", [])
        if pain_points:
            lines.append("\nPain Points:")
            for p in (pain_points if isinstance(pain_points, list) else [pain_points]):
                lines.append(f"- {p}")
    else:
        return ""

    # Append summary alongside transcript for extra signal
    if transcript_entries and summary:
        lines.append("\n\nAI SUMMARY:")
        outcome = summary.get("Outcome", "")
        if outcome:
            lines.append(f"Outcome: {outcome}")
        takeaways = summary.get("Key Takeaways", [])
        if takeaways:
            lines.append("Key Takeaways:")
            for t in takeaways:
                lines.append(f"- {t.get('topic', '')}: {t.get('key_takeaway', '')}")

    return "\n".join(lines)


def poll_sybill(progress_cb=None) -> list:
    """
    Pull new conversations from Sybill since last run, extract insights from each.
    Returns list of source_ids processed this run.
    """
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    if not config.SYBILL_API_TOKEN:
        _p("Sybill: no API token configured — skipping.")
        return []

    sources = _load_sources()
    sybill_state = sources.get("sybill", {})
    processed_ids = set(sybill_state.get("processed_ids", []))
    last_run = sybill_state.get("last_run")

    filt = _get_filter_config()

    # Date window
    if last_run:
        after_dt = last_run
    else:
        lookback = filt.get("lookback_days", 90)
        after_dt = (datetime.now(timezone.utc) - timedelta(days=lookback)).isoformat()

    _p(f"Sybill: fetching conversations since {after_dt[:10]}...")

    # Paginate through all external conversations
    all_convs = []
    page = 1
    while True:
        try:
            r = requests.get(
                f"{SYBILL_API_BASE}/v1/conversations",
                headers=_headers(),
                params={"meeting_type": "EXTERNAL", "limit": 50, "page": page},
                timeout=15
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            _p(f"Sybill: API error fetching list — {e}")
            break

        batch = data.get("conversations", [])
        if not batch:
            break

        # Filter by date — stop once we've gone past our window
        new_in_window = []
        hit_end = False
        for c in batch:
            start = c.get("startTime", "")
            if start and start < after_dt:
                hit_end = True
                break
            new_in_window.append(c)

        all_convs.extend(new_in_window)

        pagination = data.get("pagination") or {}
        has_more = pagination.get("hasMore") or pagination.get("has_more")
        if hit_end or not has_more or not batch:
            break
        page += 1
        time.sleep(0.3)

    _p(f"Sybill: {len(all_convs)} conversations found, filtering...")

    processed_this_run = []
    skipped_already = 0
    skipped_filter = 0
    skipped_no_content = 0

    for conv in all_convs:
        conv_id = conv["conversationId"]
        source_id = f"sybill_{conv_id}"
        title = conv.get("title", "Sybill call")

        if source_id in processed_ids:
            skipped_already += 1
            continue

        if not _is_relevant_call(conv, filt):
            skipped_filter += 1
            continue

        # Fetch full detail (transcript + summary)
        try:
            r2 = requests.get(
                f"{SYBILL_API_BASE}/v1/conversations/{conv_id}",
                headers=_headers(),
                timeout=20
            )
            r2.raise_for_status()
            detail = r2.json()
        except Exception as e:
            _p(f"Sybill: failed to fetch detail for '{title[:40]}' — {e}")
            continue

        # Transcript length filter
        transcript_entries = detail.get("transcript", [])
        word_count = sum(len(e.get("text", "").split()) for e in transcript_entries)
        if word_count < filt.get("min_transcript_words", 200):
            skipped_no_content += 1
            continue

        # Keyword filter
        required_kws = filt.get("require_keywords", [])
        if required_kws:
            full_text = " ".join(e.get("text", "") for e in transcript_entries).lower()
            if not any(kw.lower() in full_text for kw in required_kws):
                skipped_filter += 1
                continue

        text = _build_transcript_text(conv_id, detail)
        if not text:
            skipped_no_content += 1
            continue

        _p(f"Sybill: extracting insights from '{title[:50]}'...")
        try:
            extract_insights_from_text(text, {
                "source_id": source_id,
                "source_type": "sybill",
                "source_name": title,
                "author": "",
            })
            processed_ids.add(source_id)
            processed_this_run.append(source_id)
        except Exception as e:
            _p(f"Sybill: failed on '{title[:40]}' — {e}")

        time.sleep(0.5)

    # Save state
    sybill_state["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sybill_state["processed_ids"] = list(processed_ids)
    sources["sybill"] = sybill_state
    _save_sources(sources)

    summary_msg = f"Sybill: processed {len(processed_this_run)} new calls"
    if skipped_filter:
        summary_msg += f", skipped {skipped_filter} filtered out"
    if skipped_no_content:
        summary_msg += f", {skipped_no_content} too short"
    if skipped_already:
        summary_msg += f", {skipped_already} already processed"
    _p(summary_msg)

    return processed_this_run
