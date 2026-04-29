"""
Sybill webhook receiver.

Sybill has no pull API — it pushes call data via webhook when a meeting
finishes processing (event type: meeting.new_recording.v1).

To activate:
1. Go to Sybill dashboard → Settings → Integrations → Webhooks
2. Set the endpoint URL to: https://<your-host>/api/sybill/webhook
3. Copy the signing secret into Settings → API Keys → Sybill Token
   (we reuse the token field to store the Svix signing secret)

For local development, use ngrok to expose port 8001:
  ngrok http 8001
Then use the ngrok HTTPS URL as the webhook endpoint in Sybill.

Payload shape (meeting.new_recording.v1):
{
  "eventType": "meeting.new_recording.v1",
  "objectId": "<meeting_id>",
  "data": {
    "transcript": [{"speakerName": "...", "sentenceBody": "..."}],
    "summary": {
      "keyTakeaways": [{"heading": "...", "summary": "..."}],
      "nextSteps": ["..."],
      "outcome": "..."
    },
    "participants": [{"name": "...", "email": "...", "painPoints": [], "interests": []}]
  }
}
"""

import json
import hmac
import hashlib
import config
from modules.insight_extractor import extract_insights_from_text


DEFAULT_SYBILL_FILTER = {
    "require_external_participants": True,
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
}


def verify_svix_signature(payload_bytes: bytes, headers: dict, secret: str) -> bool:
    """
    Verify Svix webhook signature.
    Headers needed: svix-id, svix-timestamp, svix-signature
    """
    try:
        svix_id = headers.get("svix-id", "")
        svix_ts = headers.get("svix-timestamp", "")
        svix_sig = headers.get("svix-signature", "")
        if not all([svix_id, svix_ts, svix_sig, secret]):
            return False
        signed_content = f"{svix_id}.{svix_ts}.{payload_bytes.decode('utf-8')}"
        # Svix uses base64-encoded secret
        import base64
        raw_secret = base64.b64decode(secret.split("_")[-1] + "==")
        expected = hmac.new(raw_secret, signed_content.encode(), hashlib.sha256).digest()
        expected_b64 = base64.b64encode(expected).decode()
        # svix-signature may contain multiple space-separated values
        return any(sig.startswith(f"v1,{expected_b64}") for sig in svix_sig.split(" "))
    except Exception:
        return False


def _get_filter_config() -> dict:
    settings = config.load_settings()
    return {**DEFAULT_SYBILL_FILTER, **settings.get("sybill_filter", {})}


def _is_customer_call(payload: dict, filt: dict) -> bool:
    participants = payload.get("data", {}).get("participants", [])
    if filt.get("require_external_participants") and not participants:
        return False
    # Sybill doesn't expose internal/external scope in webhook payload,
    # so we rely on participant count (>1 = likely external involved)
    if len(participants) < 2:
        return False
    return True


def _passes_length_filter(payload: dict, filt: dict) -> bool:
    """Returns True if transcript has enough words to be worth processing."""
    min_words = filt.get("min_transcript_words", 0)
    if not min_words:
        return True
    transcript = payload.get("data", {}).get("transcript", [])
    word_count = sum(len(s.get("sentenceBody", "").split()) for s in transcript)
    return word_count >= min_words


def _passes_keyword_filter(payload: dict, filt: dict) -> bool:
    """Returns True if at least one required keyword appears in transcript or participant data."""
    required = filt.get("require_keywords", [])
    if not required:
        return True
    data = payload.get("data", {})
    transcript_text = " ".join(s.get("sentenceBody", "") for s in data.get("transcript", []))
    pain_points = " ".join(p for pt in data.get("participants", []) for p in pt.get("painPoints", []))
    combined = (transcript_text + " " + pain_points).lower()
    return any(kw.lower() in combined for kw in required)


def _build_transcript_text(payload: dict) -> str:
    """Convert Sybill webhook payload into a readable text block."""
    data = payload.get("data", {})
    meeting_id = payload.get("objectId", "unknown")
    summary_obj = data.get("summary", {})
    participants = data.get("participants", [])
    transcript_sentences = data.get("transcript", [])

    # Participant names
    names = [p.get("name", "") for p in participants if p.get("name")]

    lines = [f"Call ID: {meeting_id}"]
    if names:
        lines.append(f"Participants: {', '.join(names)}")
    lines.append("")

    # Transcript
    if transcript_sentences:
        lines.append("TRANSCRIPT:")
        current_speaker = None
        for s in transcript_sentences:
            speaker = s.get("speakerName", "Unknown")
            text = s.get("sentenceBody", "").strip()
            if not text:
                continue
            if speaker != current_speaker:
                lines.append(f"\n{speaker}:")
                current_speaker = speaker
            lines.append(text)
    else:
        # Fall back to structured summary
        if summary_obj:
            lines.append("CALL SUMMARY:")
            outcome = summary_obj.get("outcome", "")
            if outcome:
                lines.append(f"Outcome: {outcome}")
            takeaways = summary_obj.get("keyTakeaways", [])
            if takeaways:
                lines.append("\nKey Takeaways:")
                for t in takeaways:
                    lines.append(f"- {t.get('heading', '')}: {t.get('summary', '')}")
            next_steps = summary_obj.get("nextSteps", [])
            if next_steps:
                lines.append("\nNext Steps:")
                for ns in next_steps:
                    lines.append(f"- {ns}")
            # Participant pain points and interests
            for p in participants:
                pains = p.get("painPoints", [])
                interests = p.get("interests", [])
                if pains or interests:
                    lines.append(f"\n{p.get('name', 'Participant')}:")
                    if pains:
                        lines.append(f"  Pain points: {', '.join(pains)}")
                    if interests:
                        lines.append(f"  Interests: {', '.join(interests)}")
        else:
            return ""

    return "\n".join(lines)


def process_sybill_webhook(payload: dict, progress_cb=None) -> dict | None:
    """
    Process a Sybill webhook payload.
    Returns the extracted insight dict or None if skipped.
    """
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    event_type = payload.get("eventType", "")
    if event_type != "meeting.new_recording.v1":
        _p(f"Sybill: ignoring event type '{event_type}'")
        return None

    meeting_id = payload.get("objectId", "")
    source_id = f"sybill_{meeting_id}"

    # Check already processed
    sources_path = config.SOURCES_PATH
    if sources_path.exists():
        try:
            sources = json.loads(sources_path.read_text())
            if source_id in sources.get("sybill", {}).get("processed_ids", []):
                _p(f"Sybill: {meeting_id} already processed — skipping.")
                return None
        except Exception:
            pass

    filt = _get_filter_config()
    if not _is_customer_call(payload, filt):
        _p(f"Sybill: {meeting_id} filtered out (likely internal/short call).")
        return None

    if not _passes_length_filter(payload, filt):
        _p(f"Sybill: {meeting_id} transcript too short — skipping.")
        return None

    if not _passes_keyword_filter(payload, filt):
        _p(f"Sybill: {meeting_id} has no relevant keywords — skipping.")
        return None

    participants = payload.get("data", {}).get("participants", [])
    call_title = f"Sybill call ({', '.join(p.get('name','') for p in participants[:2] if p.get('name'))})"

    text = _build_transcript_text(payload)
    if not text:
        _p(f"Sybill: {meeting_id} has no usable content — skipping.")
        return None

    _p(f"Sybill: extracting insights from '{call_title}'...")
    try:
        result = extract_insights_from_text(text, {
            "source_id": source_id,
            "source_type": "sybill",
            "source_name": call_title,
            "author": "",
        })

        # Mark as processed
        if sources_path.exists():
            try:
                sources = json.loads(sources_path.read_text())
            except Exception:
                sources = {}
        else:
            sources = {}

        sybill_state = sources.get("sybill", {})
        processed = sybill_state.get("processed_ids", [])
        processed.append(source_id)
        sybill_state["processed_ids"] = processed
        sources["sybill"] = sybill_state
        sources_path.write_text(json.dumps(sources, indent=2))

        _p(f"Sybill: insights extracted from '{call_title}'.")
        return result
    except Exception as e:
        _p(f"Sybill: failed on '{call_title}' — {e}")
        return None
