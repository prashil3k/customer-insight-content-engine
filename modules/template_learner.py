import json
import time
import threading
import requests
from bs4 import BeautifulSoup
import config
from modules.model_manager import create_message

_formats_lock = threading.Lock()


def _load_formats() -> dict:
    if config.FORMATS_PATH.exists():
        return json.loads(config.FORMATS_PATH.read_text())
    return {"formats": []}


def _save_formats(data: dict):
    config.FORMATS_PATH.write_text(json.dumps(data, indent=2))


def _fetch_article(url: str) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; StorylaneCE/1.0)"}
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", ".sidebar", ".ad"]):
            tag.decompose()

        article = soup.find("article") or soup.find("main") or soup.find(class_=["post-content", "article-content", "entry-content"])
        if article:
            text = article.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)

        lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 20]
        return "\n".join(lines[:400])
    except Exception as e:
        return f"[Error fetching {url}: {e}]"


def learn_from_url(url: str, label: str = "", progress_cb=None) -> dict | None:
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    data = _load_formats()
    if any(f["url"] == url for f in data["formats"]):
        return None

    _p(f"Fetching article from {url}...")
    article_text = _fetch_article(url)
    if article_text.startswith("[Error"):
        return {"error": article_text}

    _p("Extracting content format with Claude...")

    prompt = f"""You are a content strategist studying high-quality articles to understand what makes them work. Analyze this article and extract a reusable content format.

URL: {url}
LABEL: {label or "unlabelled"}

ARTICLE CONTENT:
{article_text[:12000]}

Extract TWO layers of insight that together would let a writer produce articles with the same quality and feel:

LAYER 1 — STRUCTURAL TEMPLATE (the how):
What exactly goes where. Not "has an intro" — instead: does the intro lead with a claim, a scenario, a question, a stat? How long is it? What does the first H2 tackle? How is evidence introduced — before or after the claim? What's the ratio of prose to lists? How does the conclusion/CTA work? Be specific enough that someone could use this as a blueprint.

LAYER 2 — UNDERLYING APPROACH / MINDSET (the why):
What is the writer optimising for? What does the reader feel reading this — informed, challenged, validated? Why does this structure work for this type of content specifically? What are the non-obvious judgment calls — why does a stat appear early, why does the tone shift here, why does the writer use second person? What would this writer never do?

Return a JSON object with:
- "url": the URL
- "label": provided label or inferred title
- "inferred_angle": One of: thought_leadership, listicle, opinion, data_led, how_to, comparison, digital_pr
- "structural_template": Object with:
    - "hook_type": how the article opens (claim | scenario | question | stat | story | counter-intuitive)
    - "hook_description": what the hook actually does in this article (2-3 sentences)
    - "structure_outline": Array of section descriptions, e.g. ["H2: Problem framing with specific scenario (150 words)", "H2: 3-item framework (each item: claim + proof + example, ~100 words)"]
    - "evidence_pattern": How evidence is used (before/after claims, inline, footnoted, etc.)
    - "prose_vs_lists": Ratio and when each is used
    - "conclusion_pattern": How the article ends and what the CTA does
    - "avg_section_length": Approximate word count per section
    - "total_length_estimate": Approximate total word count
- "mindset": Object with:
    - "reader_goal": What the reader wants from this article (1-2 sentences)
    - "writer_optimising_for": What the writer is trying to achieve (persuade, inform, validate, challenge)
    - "tone_character": How the article sounds — direct, conversational, authoritative, provocative, etc.
    - "non_obvious_choices": Array of specific choices in this article that aren't obvious — and why they work
    - "what_this_writer_avoids": Array of things this style deliberately avoids
    - "key_insight": The one thing that makes this article good, distilled to 1-2 sentences
- "use_when": When to use this format (what topic types, what angles, what reader states)

Return ONLY the JSON object."""

    response = create_message("sonnet", max_tokens=2500, messages=[{"role": "user", "content": prompt}])

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()

    fmt = json.loads(raw)
    fmt["added_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with _formats_lock:
        data = _load_formats()   # re-read inside lock so concurrent saves aren't lost
        data["formats"].append(fmt)
        _save_formats(data)
    _p("Format extracted and saved.")
    return fmt


def learn_from_text(text: str, label: str = "", angle_hint: str = "", progress_cb=None) -> dict | None:
    """Process a pasted framework/brief into a structured format entry."""
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    data = _load_formats()
    _p("Extracting framework structure with Claude...")

    angle_note = f"\nThe user believes this maps to the '{angle_hint}' angle — use that unless clearly wrong." if angle_hint else ""

    prompt = f"""You are a content strategist. A user has pasted a writing framework or content brief they want to use as a structural guide when writing articles.{angle_note}

PASTED FRAMEWORK:
{text[:8000]}

Your job: extract this into a structured format that a draft generator can use as a writing blueprint.

Return a JSON object with:
- "url": "text://{int(time.time())}"
- "label": "{label or 'Pasted Framework'}"
- "source_type": "text"
- "inferred_angle": One of: thought_leadership, listicle, opinion, data_led, how_to, comparison, digital_pr — pick the best fit based on the framework's intent
- "structural_template": Object with:
    - "hook_type": the recommended opening style (claim | scenario | question | stat | story | counter-intuitive)
    - "hook_description": what the opening should do (2-3 sentences)
    - "structure_outline": Array of section descriptions following this framework, e.g. ["H2: Quick summary / LLM-friendly intro (100 words)", "H2: Brand positioning section with social proof"]
    - "evidence_pattern": how evidence and social proof should be used
    - "prose_vs_lists": guidance on when to use prose vs lists/tables
    - "conclusion_pattern": how the article should end and what the CTA should do
    - "avg_section_length": approximate word count per section
    - "total_length_estimate": approximate total word count
- "mindset": Object with:
    - "reader_goal": what the reader wants from this type of article
    - "writer_optimising_for": what this framework is optimising for (e.g. LLM visibility, conversion, authority)
    - "tone_character": recommended tone
    - "non_obvious_choices": Array of specific structural choices from this framework and why they work
    - "what_this_writer_avoids": Array of things this framework explicitly avoids
    - "key_insight": The core idea behind this framework in 1-2 sentences
- "use_when": When to apply this framework (what article types, what goals)

Return ONLY the JSON object."""

    response = create_message("haiku", max_tokens=2500, messages=[{"role": "user", "content": prompt}])

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()

    fmt = json.loads(raw)
    fmt["added_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fmt["source_type"] = "text"

    with _formats_lock:
        data = _load_formats()   # re-read inside lock so concurrent saves aren't lost
        data["formats"].append(fmt)
        _save_formats(data)
    _p("Framework extracted and saved.")
    return fmt


def save_raw_instructions(text: str, label: str = "") -> dict | None:
    """
    Store raw writing instructions directly — no analysis, no Claude call.
    These travel with the training library and get injected into draft prompts
    as direct writing rules (not structural templates to learn from).
    """
    import uuid
    entry = {
        "url": f"instructions://{int(time.time())}",
        "label": label or "Writing Instructions",
        "source_type": "instructions",
        "inferred_angle": "all",
        "content": text.strip(),
        "structural_template": {},
        "mindset": {},
        "use_when": "Always — these are direct writing rules, not a structural template.",
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with _formats_lock:
        data = _load_formats()
        data["formats"].append(entry)
        _save_formats(data)
    return entry


def get_formats() -> list:
    return _load_formats().get("formats", [])


def delete_format(url: str) -> bool:
    data = _load_formats()
    before = len(data["formats"])
    data["formats"] = [f for f in data["formats"] if f.get("url") != url]
    if len(data["formats"]) < before:
        _save_formats(data)
        return True
    return False


def get_format_context_for_angle(angle: str) -> str:
    formats = get_formats()
    if not formats:
        return ""

    matching = [f for f in formats if f.get("inferred_angle") == angle]
    use_formats = matching[:2] if matching else formats[:2]

    if not use_formats:
        return ""

    lines = ["=== CONTENT FORMAT REFERENCE (from training library) ==="]
    for fmt in use_formats:
        tmpl = fmt.get("structural_template", {})
        mind = fmt.get("mindset", {})
        lines.append(f"\nFormat from: {fmt.get('url', 'unknown')} [{fmt.get('inferred_angle', '')}]")
        lines.append(f"Hook: {tmpl.get('hook_type', '')} — {tmpl.get('hook_description', '')}")
        lines.append(f"Structure: {'; '.join(tmpl.get('structure_outline', [])[:5])}")
        lines.append(f"Mindset: {mind.get('writer_optimising_for', '')} | Tone: {mind.get('tone_character', '')}")
        lines.append(f"Key insight: {mind.get('key_insight', '')}")
        avoids = mind.get("what_this_writer_avoids", [])
        if avoids:
            lines.append(f"Avoid: {', '.join(avoids[:3])}")
    lines.append("=== END FORMAT REFERENCE ===")
    return "\n".join(lines)
