"""
Internal Link Library

Maintains an index of Storylane's own content (blog posts, docs pages, feature pages)
so the draft generator can inject real, accurate internal links rather than inventing URLs.

Usage:
- User adds URLs via Settings → Internal Links
- Each URL is scraped and indexed with title, summary, keywords, pillar, and type
- At draft generation time, get_relevant_links(topic, pillar) returns the best matches
- Links are passed to the draft prompt as a block the model uses naturally

Storage: data/link_index.json
"""

import json
import time
from pathlib import Path
import config
from modules.model_manager import create_message


LINK_INDEX_PATH = config.DATA_DIR / "link_index.json"

LINK_TYPES = ("blog", "docs", "feature", "pricing", "comparison", "other")


def _load_index() -> dict:
    if LINK_INDEX_PATH.exists():
        try:
            return json.loads(LINK_INDEX_PATH.read_text())
        except Exception:
            pass
    return {"links": []}


def _save_index(data: dict):
    LINK_INDEX_PATH.write_text(json.dumps(data, indent=2))


def _fetch_url(url: str) -> str:
    """Fetch and clean page text. Reuses same approach as company_brain."""
    try:
        import requests
        from bs4 import BeautifulSoup
        headers = {"User-Agent": "Mozilla/5.0 (compatible; StorylaneCE/1.0)"}
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # Grab title before stripping tags
        title_tag = soup.find("title")
        page_title = title_tag.get_text(strip=True) if title_tag else ""
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [l for l in text.splitlines() if len(l.strip()) > 30]
        return page_title, "\n".join(lines[:200])
    except Exception as e:
        return "", f"[Could not fetch {url}: {e}]"


def _infer_link_type(url: str, title: str) -> str:
    u = url.lower()
    t = title.lower()
    if "/blog/" in u or "/articles/" in u:
        return "blog"
    if "/docs/" in u or "/documentation/" in u or "/guide/" in u or "/help/" in u:
        return "docs"
    if "/pricing" in u:
        return "pricing"
    if "/vs-" in u or "-vs-" in u or "alternative" in u or "compare" in u:
        return "comparison"
    if "feature" in u or "product" in u or any(w in t for w in ["how to", "guide", "demo"]):
        return "feature"
    return "other"


def index_url(url: str, progress_cb=None) -> dict | None:
    """
    Scrape a URL and extract link metadata via Claude.
    Returns the indexed link dict, or None if already indexed.
    """
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    data = _load_index()
    existing_urls = {l["url"] for l in data["links"]}
    if url in existing_urls:
        _p(f"Already indexed: {url}")
        # Return existing entry
        return next((l for l in data["links"] if l["url"] == url), None)

    _p(f"Fetching {url}...")
    page_title, page_text = _fetch_url(url)

    if page_text.startswith("[Could not fetch"):
        _p(f"Failed to fetch: {url}")
        return None

    _p("Extracting metadata with Claude...")
    pillar_names = ", ".join(config.DEFAULT_PILLARS)

    prompt = f"""You are indexing a Storylane content page for an internal link library. Extract structured metadata from the page content below.

URL: {url}
PAGE TITLE: {page_title}

PAGE CONTENT:
{page_text[:8000]}

Return a JSON object with:
- "title": Clean, readable page title (use the page title if good, improve if generic)
- "summary": 1-2 sentence description of what this page covers and who it's for
- "keywords": Array of 5-10 keywords or phrases this page is relevant for (what would someone be reading about when this link would be helpful?)
- "pillar": The single most relevant content pillar from this list: {pillar_names}
- "anchor_suggestions": Array of 2-3 natural anchor text phrases someone would use to link to this page (e.g. "interactive demo software", "how to build a product demo")

Return ONLY the JSON object."""

    response = create_message("haiku", max_tokens=600, messages=[{"role": "user", "content": prompt}])
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()

    extracted = json.loads(raw)

    link = {
        "url": url,
        "title": extracted.get("title") or page_title or url,
        "summary": extracted.get("summary", ""),
        "keywords": extracted.get("keywords", []),
        "anchor_suggestions": extracted.get("anchor_suggestions", []),
        "pillar": extracted.get("pillar", ""),
        "type": _infer_link_type(url, page_title),
        "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    data["links"].append(link)
    _save_index(data)

    _p(f"Indexed: {link['title']}")
    return link


def remove_link(url: str) -> bool:
    """Remove a link from the index by URL."""
    data = _load_index()
    before = len(data["links"])
    data["links"] = [l for l in data["links"] if l["url"] != url]
    if len(data["links"]) < before:
        _save_index(data)
        return True
    return False


def get_all_links() -> list:
    return _load_index()["links"]


def get_relevant_links(topic: str, pillar: str = "", num: int = 4) -> list:
    """
    Return the most relevant internal links for a given topic.
    Scores by: keyword overlap with topic words, pillar match, link type.
    """
    all_links = get_all_links()
    if not all_links:
        return []

    topic_words = set(w.lower() for w in topic.split() if len(w) > 3)

    def score(link):
        s = 0.0
        # Keyword overlap
        kw_text = " ".join(link.get("keywords", [])).lower()
        summary_text = (link.get("summary", "") + " " + link.get("title", "")).lower()
        for word in topic_words:
            if word in kw_text:
                s += 0.4
            if word in summary_text:
                s += 0.2
        # Pillar match
        if pillar and link.get("pillar", "").lower() == pillar.lower():
            s += 1.0
        # Prefer blog and feature pages for inline links
        if link.get("type") in ("blog", "feature"):
            s += 0.3
        return s

    scored = sorted(all_links, key=score, reverse=True)
    # Only return links with at least some relevance score
    relevant = [l for l in scored if score(l) > 0]
    return relevant[:num]


def format_links_for_prompt(links: list) -> str:
    """Format internal links as a block for injection into the draft prompt."""
    if not links:
        return ""
    lines = ["INTERNAL LINKS — weave 2-3 of these naturally into the article where relevant. Use the suggested anchor text or a natural variation. Do not force links that don't fit."]
    for l in links:
        anchors = ", ".join(f'"{a}"' for a in l.get("anchor_suggestions", [])[:2])
        lines.append(
            f"- [{l['type'].upper()}] {l['title']}\n"
            f"  URL: {l['url']}\n"
            f"  About: {l['summary']}\n"
            f"  Anchor text: {anchors}"
        )
    return "\n".join(lines)
