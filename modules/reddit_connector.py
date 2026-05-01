import time
import urllib.request
import urllib.parse
import json

# Subreddits most relevant for B2B SaaS / demo / GTM content
DEFAULT_SUBREDDITS = [
    "sales", "marketing", "SaaS", "b2bmarketing", "startups", "salestechnology",
]

_HEADERS = {"User-Agent": "storylane-content-engine/1.0"}


def _fetch(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fetch_posts(query: str, subreddits: list = None, limit_per_sub: int = 10, time_filter: str = "year") -> list:
    """
    Fetch Reddit posts matching `query` from the given subreddits.
    Returns list of dicts with keys: title, body, url, subreddit, score.
    No API key required — uses public JSON endpoint.
    """
    subreddits = subreddits or DEFAULT_SUBREDDITS
    posts = []
    q = urllib.parse.quote(query)

    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/search.json?q={q}&restrict_sr=1&sort=relevance&t={time_filter}&limit={limit_per_sub}"
        data = _fetch(url)
        if not data:
            continue
        for child in data.get("data", {}).get("children", []):
            p = child.get("data", {})
            if p.get("score", 0) < 5:
                continue
            posts.append({
                "title": p.get("title", ""),
                "body": p.get("selftext", ""),
                "url": f"https://reddit.com{p.get('permalink', '')}",
                "subreddit": sub,
                "score": p.get("score", 0),
            })
        time.sleep(0.5)  # be polite

    return posts


def posts_to_text(posts: list, max_chars: int = 15000) -> str:
    lines = []
    for p in posts:
        block = f"[r/{p['subreddit']} · {p['score']} upvotes]\n{p['title']}"
        if p["body"] and p["body"] != "[deleted]":
            block += f"\n{p['body'][:800]}"
        lines.append(block)
    text = "\n\n---\n\n".join(lines)
    return text[:max_chars]


def ingest_reddit(query: str, subreddits: list = None, progress_cb=None) -> dict:
    """
    Fetch Reddit posts for `query`, bundle them, and push to insight_extractor.
    Returns {"ok": True, "posts_found": N} or {"ok": False, "error": str}.
    """
    from modules.insight_extractor import extract_insights_from_text
    import uuid

    if progress_cb:
        progress_cb(f"Fetching Reddit posts for: {query}...")

    posts = fetch_posts(query, subreddits=subreddits)
    if not posts:
        return {"ok": False, "error": "No posts found"}

    if progress_cb:
        progress_cb(f"Found {len(posts)} posts — extracting insights...")

    text = posts_to_text(posts)
    source_id = f"reddit_{urllib.parse.quote(query[:40])}_{int(time.time())}"

    result = extract_insights_from_text(text, {
        "source_id": source_id,
        "source_type": "reddit",
        "source_name": f"Reddit: {query}",
        "author": "reddit",
    })

    if progress_cb:
        progress_cb("DONE" if result else "DONE — already processed or no insights extracted")

    return {"ok": True, "posts_found": len(posts)}
