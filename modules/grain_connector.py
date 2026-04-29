"""
Grain REST API poller.

Pulls recordings since last run, extracts transcript or AI summary,
filters to customer-facing calls, and feeds each into extract_insights_from_text().

Uses cursor-based pagination. Saves last-run timestamp + processed IDs to sources.json.
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import config
from modules.insight_extractor import extract_insights_from_text

GRAIN_API_BASE = "https://api.grain.com"
GRAIN_API_VERSION = "2025-10-31"

# Default call-type filters (can be overridden in settings.json under grain_filter)
DEFAULT_GRAIN_FILTER = {
    "require_external_participants": True,   # skip internal-only meetings
    "min_external_participants": 1,
    "exclude_title_patterns": [             # case-insensitive; skip calls whose title contains any of these
        "scrum", "standup", "stand-up", "1:1", "one on one",
        "all hands", "all-hands", "offsite", "interview",
    ],
    "require_keywords": [                   # at least one must appear in transcript or title (empty = no filter)
        "demo", "onboarding", "pricing", "objection", "competitor",
        "pain point", "use case", "buyer", "trial", "evaluation",
    ],
    "min_duration_minutes": 10,             # skip calls shorter than this
    "min_transcript_words": 200,            # skip calls with too little transcript content
    "lookback_days": 30,    # on first run, how many days back to pull
}


def _headers():
    return {
        "Authorization": f"Bearer {config.GRAIN_API_TOKEN}",
        "Public-Api-Version": GRAIN_API_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{GRAIN_API_BASE}{path}",
        data=data, method="POST", headers=_headers()
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _get(path: str) -> bytes:
    req = urllib.request.Request(
        f"{GRAIN_API_BASE}{path}",
        headers={k: v for k, v in _headers().items() if k != "Content-Type"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


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
    return {**DEFAULT_GRAIN_FILTER, **settings.get("grain_filter", {})}


def _is_customer_call(recording: dict, filt: dict) -> bool:
    """Returns True if this recording looks like a customer/prospect call worth processing."""
    if filt.get("require_external_participants"):
        ext = [p for p in recording.get("participants", []) if p.get("scope") == "external"]
        if len(ext) < filt.get("min_external_participants", 1):
            return False

    title = recording.get("title", "").lower()
    for pattern in filt.get("exclude_title_patterns", []):
        if pattern.lower() in title:
            return False

    # Duration filter
    min_dur = filt.get("min_duration_minutes", 0)
    if min_dur:
        duration_min = recording.get("duration_ms", 0) / 60000
        if duration_min < min_dur:
            return False

    return True


def _passes_length_filter(transcript: str, filt: dict) -> bool:
    """Returns True if transcript has enough content to be worth processing."""
    min_words = filt.get("min_transcript_words", 0)
    if not min_words:
        return True
    word_count = len(transcript.split())
    return word_count >= min_words


def _passes_keyword_filter(text: str, title: str, filt: dict) -> bool:
    """Returns True if at least one required keyword appears in transcript or title."""
    required = filt.get("require_keywords", [])
    if not required:
        return True  # no filter configured — pass everything
    combined = (text + " " + title).lower()
    return any(kw.lower() in combined for kw in required)


def _fetch_transcript_text(recording_id: str) -> str:
    """Fetch plain-text transcript for a recording. Returns empty string if unavailable."""
    try:
        raw = _get(f"/_/public-api/v2/recordings/{recording_id}/transcript?format=txt")
        text = raw.decode("utf-8").strip()
        # Grain returns "[]" or empty when transcript isn't ready
        if text in ("[]", "", "null"):
            return ""
        return text
    except Exception:
        return ""


def _build_text_from_recording(recording: dict, transcript: str) -> str:
    """Assemble a rich text block from recording metadata + content."""
    title = recording.get("title", "Unknown call")
    date = recording.get("start_datetime", "")[:10]
    source = recording.get("source", "")
    duration_min = round(recording.get("duration_ms", 0) / 60000)

    participants = recording.get("participants", [])
    ext_names = [p.get("name", "") for p in participants if p.get("scope") == "external" and p.get("name")]
    int_names = [p.get("name", "") for p in participants if p.get("scope") == "internal" and p.get("name")]

    lines = [
        f"Call: {title}",
        f"Date: {date} | Source: {source} | Duration: {duration_min} min",
    ]
    if ext_names:
        lines.append(f"Customer/External: {', '.join(ext_names)}")
    if int_names:
        lines.append(f"Storylane team: {', '.join(int_names)}")
    lines.append("")

    if transcript:
        lines.append("TRANSCRIPT:")
        lines.append(transcript)
    else:
        # Fall back to AI summary
        summary = recording.get("ai_summary")
        if summary:
            summary_text = summary.get("text", "") if isinstance(summary, dict) else str(summary)
            if summary_text:
                lines.append("CALL SUMMARY (AI-generated):")
                lines.append(summary_text)
            else:
                return ""  # nothing to process
        else:
            return ""  # no content at all

    return "\n".join(lines)


def poll_grain(progress_cb=None) -> list:
    """
    Pull new recordings from Grain since last run, extract insights from each.
    Returns list of source_ids processed this run.
    """
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    if not config.GRAIN_API_TOKEN:
        _p("Grain: no API token configured — skipping.")
        return []

    sources = _load_sources()
    grain_state = sources.get("grain", {})
    processed_ids = set(grain_state.get("processed_ids", []))
    last_run = grain_state.get("last_run")

    filt_cfg = _get_filter_config()

    # Determine time window
    if last_run:
        after_dt = last_run
    else:
        lookback = filt_cfg.get("lookback_days", 30)
        after_dt = (datetime.now(timezone.utc) - timedelta(days=lookback)).strftime("%Y-%m-%dT%H:%M:%SZ")

    _p(f"Grain: fetching recordings since {after_dt[:10]}...")

    # Paginate through all recordings in the window
    all_recordings = []
    cursor = None
    while True:
        body = {
            "filter": {"after_datetime": after_dt},
            "include": {"participants": True, "ai_summary": True},
        }
        if cursor:
            body["cursor"] = cursor

        try:
            resp = _post("/_/public-api/v2/recordings", body)
        except Exception as e:
            _p(f"Grain: API error — {e}")
            break

        batch = resp.get("recordings", [])
        all_recordings.extend(batch)
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break

    _p(f"Grain: {len(all_recordings)} recordings found, filtering for customer calls...")

    processed_this_run = []
    skipped_no_content = 0
    skipped_internal = 0
    skipped_already = 0

    for rec in all_recordings:
        rec_id = rec["id"]
        source_id = f"grain_{rec_id}"

        if source_id in processed_ids:
            skipped_already += 1
            continue

        if not _is_customer_call(rec, filt_cfg):
            skipped_internal += 1
            continue

        # Fetch transcript (may be empty if still processing)
        transcript = _fetch_transcript_text(rec_id)
        time.sleep(0.3)  # gentle rate limiting

        title = rec.get("title", "Grain call")

        # Transcript length filter
        if not _passes_length_filter(transcript, filt_cfg):
            skipped_no_content += 1
            continue

        # Keyword inclusion filter — check title + transcript
        if not _passes_keyword_filter(transcript, title, filt_cfg):
            skipped_internal += 1
            continue

        text = _build_text_from_recording(rec, transcript)
        if not text:
            skipped_no_content += 1
            continue
        _p(f"Grain: extracting insights from '{title[:50]}'...")

        try:
            extract_insights_from_text(text, {
                "source_id": source_id,
                "source_type": "grain",
                "source_name": title,
                "author": "",
            })
            processed_ids.add(source_id)
            processed_this_run.append(source_id)
        except Exception as e:
            _p(f"Grain: failed on '{title[:40]}' — {e}")

        time.sleep(0.5)

    # Save updated state
    grain_state["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    grain_state["processed_ids"] = list(processed_ids)
    sources["grain"] = grain_state
    _save_sources(sources)

    summary = f"Grain: processed {len(processed_this_run)} new calls"
    if skipped_internal:
        summary += f", skipped {skipped_internal} internal"
    if skipped_no_content:
        summary += f", {skipped_no_content} pending transcript"
    if skipped_already:
        summary += f", {skipped_already} already processed"
    _p(summary)

    return processed_this_run
