#!/usr/bin/env python3
"""
Storylane Demo Classifier
=========================
A persistent demo discovery and classification tool. Builds a local index
of demos from the Storylane customer showcase that grows with each scan.

Usage:
    python3 run.py                      # Sync: scrape showcase, walk+classify NEW demos only
    python3 run.py --sync               # Same as above (explicit)
    python3 run.py --rescan             # Re-walk and re-classify ALL demos in the index
    python3 run.py --rescan "Acme"      # Re-scan a specific demo by name (fuzzy match)
    python3 run.py --search "onboard"   # Search the index by name/category/type/tags
    python3 run.py --list               # List all demos in the index
    python3 run.py --list --type "Strong Storytelling"  # Filter by classification type
    python3 run.py --list --min-score 7 # Filter by minimum overall score
    python3 run.py --import urls.txt    # Bulk import demo URLs from a file
    python3 run.py --import-urls "url1,url2,..."  # Import comma-separated URLs
    python3 run.py --stats              # Show index statistics
    python3 run.py --demo-url URL       # Process a single demo URL directly
    python3 run.py --limit 5            # Only process first 5 NEW demos
    python3 run.py --scrape-only        # Only scrape demo URLs (no walking/classifying)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SHOWCASE_URL = "https://www.storylane.io/customer-showcase"
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
OUTPUT_DIR = Path(__file__).parent / "output"
INDEX_FILE = Path(__file__).parent / "demo_index.json"
QUERY_KNOWLEDGE_FILE = Path(__file__).parent / "query_knowledge.json"
CLASSIFICATION_CRITERIA_FILE = Path(__file__).parent / "classification_criteria.txt"
CUSTOM_RUBRICS_DIR = Path(__file__).parent / "rubrics"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# Model future-proofing: known models, runtime detection, fallback chain
# ---------------------------------------------------------------------------

KNOWN_MODELS = {
    "haiku": [
        {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5"},
    ],
    "sonnet": [
        {"id": "claude-sonnet-4-6-20250627", "label": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4"},
    ],
}

MODEL_FALLBACKS = {
    "claude-haiku-4-5-20251001": [],
    "claude-sonnet-4-6-20250627": ["claude-sonnet-4-20250514"],
    "claude-sonnet-4-20250514": ["claude-sonnet-4-6-20250627"],
}

_detected_models = {"haiku": None, "sonnet": None}


def detect_available_models(api_key: str) -> dict:
    import anthropic
    detected = {}
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.models.list(limit=100)
        available_ids = {m.id for m in response.data}
        for tier, models in KNOWN_MODELS.items():
            for model in models:
                if model["id"] in available_ids:
                    detected[tier] = model["id"]
                    break
    except Exception as e:
        print(f"   Warning: Could not detect models via API: {e}")
    return detected


def get_model(tier: str, api_key: str) -> str:
    global _detected_models
    if _detected_models.get(tier):
        return _detected_models[tier]
    if api_key:
        detected = detect_available_models(api_key)
        _detected_models.update(detected)
        if tier in detected:
            return detected[tier]
    return KNOWN_MODELS[tier][0]["id"]


def call_with_fallback(client, model_id: str, **kwargs):
    try:
        return client.messages.create(model=model_id, **kwargs)
    except Exception as e:
        error_str = str(e).lower()
        if "not found" in error_str or "deprecated" in error_str or "not available" in error_str:
            fallbacks = MODEL_FALLBACKS.get(model_id, [])
            for fb in fallbacks:
                print(f"   Warning: Model {model_id} unavailable, trying {fb}...")
                try:
                    return client.messages.create(model=fb, **kwargs)
                except Exception:
                    continue
        raise


# Playwright settings
HEADLESS = True
VIEWPORT = {"width": 1440, "height": 900}
STEP_TIMEOUT_MS = 8000
PAGE_LOAD_TIMEOUT_MS = 30000
STEP_TRANSITION_WAIT_MS = 1500
MAX_STEPS_PER_DEMO = 40


# ---------------------------------------------------------------------------
# Persistent Demo Index
# ---------------------------------------------------------------------------

def _demo_key(url: str) -> str:
    """Generate a stable key from a demo URL for deduplication."""
    # Strip protocol, trailing slashes, query params
    clean = url.split("?")[0].split("#")[0].rstrip("/")
    # Use the path portion as key
    if "://" in clean:
        clean = clean.split("://", 1)[1]
    return clean.lower()


def load_index() -> dict:
    """Load the persistent demo index from disk."""
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"version": 2, "last_sync": None, "demos": {}}


def save_index(index: dict):
    """Save the demo index to disk."""
    index["last_sync"] = datetime.now().isoformat()
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX_FILE, "w") as f:
        json.dump(index, f, indent=2)


def merge_demo_into_index(index: dict, demo_data: dict, force: bool = False) -> bool:
    """
    Merge a demo into the index. Returns True if this is a new or updated entry.
    If force=True, overwrites existing scan data (for --rescan).
    """
    # Try multiple URL fields for keying
    key = None
    for url_field in ["demo_url", "demo_iframe_url", "showcase_url"]:
        url = demo_data.get(url_field, "")
        if url:
            key = _demo_key(url)
            break

    if not key:
        return False

    existing = index["demos"].get(key)
    is_new = existing is None

    if is_new:
        entry = {
            "key": key,
            "name": demo_data.get("name", "Unknown"),
            "showcase_url": demo_data.get("showcase_url", ""),
            "demo_url": demo_data.get("demo_url", "") or demo_data.get("demo_iframe_url", ""),
            "live_preview_url": demo_data.get("live_preview_url", ""),
            "category": demo_data.get("category", ""),
            "is_accessible": demo_data.get("is_accessible", True),
            "is_gated": demo_data.get("is_gated", False),
            "error": demo_data.get("error", ""),
            "total_steps": demo_data.get("total_steps", 0),
            "steps_captured": demo_data.get("steps_captured", 0),
            "steps_text": demo_data.get("steps_text", []),
            "classification": demo_data.get("classification", {}),
            "tags": demo_data.get("tags", []),
            "source": demo_data.get("source", "showcase"),
            "discovered_at": datetime.now().isoformat(),
            "last_scanned_at": None,
            "scan_count": 0,
            "screenshots_captured": demo_data.get("screenshots_captured", False),
            "has_chapters": demo_data.get("has_chapters", False),
            "chapter_count": demo_data.get("chapter_count", 0),
        }
    else:
        entry = existing.copy()
        # Always update metadata
        if demo_data.get("name"):
            entry["name"] = demo_data["name"]
        if demo_data.get("category"):
            entry["category"] = demo_data["category"]
        if demo_data.get("showcase_url"):
            entry["showcase_url"] = demo_data["showcase_url"]
        if demo_data.get("live_preview_url"):
            entry["live_preview_url"] = demo_data["live_preview_url"]
        if demo_data.get("demo_url") or demo_data.get("demo_iframe_url"):
            entry["demo_url"] = demo_data.get("demo_url", "") or demo_data.get("demo_iframe_url", "")

    # Update scan data if we have new results or force rescan
    has_scan_data = demo_data.get("steps_text") or demo_data.get("classification")
    if has_scan_data and (force or is_new or not existing.get("last_scanned_at")):
        if demo_data.get("steps_text"):
            entry["steps_text"] = demo_data["steps_text"]
        if demo_data.get("steps_captured"):
            entry["steps_captured"] = demo_data["steps_captured"]
            entry["total_steps"] = demo_data.get("total_steps", 0)
        if demo_data.get("screenshots_captured") is not None:
            entry["screenshots_captured"] = demo_data["screenshots_captured"]
        if demo_data.get("has_chapters"):
            entry["has_chapters"] = demo_data["has_chapters"]
            entry["chapter_count"] = demo_data.get("chapter_count", 0)
        if demo_data.get("classification"):
            entry["classification"] = demo_data["classification"]
        if demo_data.get("is_accessible") is not None:
            entry["is_accessible"] = demo_data["is_accessible"]
        if demo_data.get("is_gated") is not None:
            entry["is_gated"] = demo_data["is_gated"]
        entry["error"] = demo_data.get("error", "")
        entry["last_scanned_at"] = datetime.now().isoformat()
        entry["scan_count"] = entry.get("scan_count", 0) + 1

    # Merge user tags
    if demo_data.get("tags"):
        existing_tags = set(entry.get("tags", []))
        existing_tags.update(demo_data["tags"])
        entry["tags"] = sorted(existing_tags)

    # Merge auto-tags from classification
    auto_tags = demo_data.get("classification", {}).get("_auto_tags", [])
    if auto_tags:
        existing_tags = set(entry.get("tags", []))
        existing_tags.update(auto_tags)
        entry["tags"] = sorted(existing_tags)

    # Always compute a smart display_name; enrich with suggestions if classified
    if entry.get("classification") and entry["classification"].get("type"):
        enrich_demo_with_suggestions(entry)
    else:
        entry["display_name"] = build_display_name(entry)

    index["demos"][key] = entry
    return is_new


def check_missing_screenshots(index: dict) -> list:
    """Return demos where screenshots were previously captured but files are now missing."""
    missing = []
    for key, demo in index["demos"].items():
        if not demo.get("screenshots_captured"):
            continue
        demo_dir = SCREENSHOTS_DIR / _safe_filename(demo.get("name", ""))
        if not demo_dir.exists() or not any(demo_dir.glob("*.png")):
            missing.append({
                "key": key,
                "name": demo.get("name", key),
                "demo_url": demo.get("demo_url", ""),
            })
    return missing


def search_index(index: dict, query: str) -> list:
    """Fuzzy search the index by name, category, type, tags, URL."""
    query_lower = query.lower()
    results = []
    for key, demo in index["demos"].items():
        score = 0
        searchable = " ".join([
            demo.get("name", ""),
            demo.get("category", ""),
            demo.get("classification", {}).get("type", ""),
            demo.get("classification", {}).get("summary", ""),
            " ".join(demo.get("tags", [])),
            demo.get("demo_url", ""),
            demo.get("showcase_url", ""),
        ]).lower()

        # Exact substring match
        if query_lower in searchable:
            score += 10
        # Word-level matching
        for word in query_lower.split():
            if word in searchable:
                score += 5
            # Partial match
            for field_word in searchable.split():
                if word in field_word:
                    score += 2

        if score > 0:
            results.append((score, demo))

    results.sort(key=lambda x: -x[0])
    return [r[1] for r in results]


def smart_query_index(index: dict, query: str, api_key: str = None) -> list:
    """
    Use Claude to semantically search the index. Understands natural language queries like
    'find me an onboarding demo for enterprise' or 'demos with strong storytelling and proof'.
    Falls back to keyword search if no API key.
    """
    effective_key = api_key or ANTHROPIC_API_KEY
    if not effective_key:
        print("   No API key -- falling back to keyword search")
        return search_index(index, query)

    demos = list(index["demos"].values())
    if not demos:
        return []

    # Build a compact summary of all demos for the model
    demo_summaries = []
    for i, d in enumerate(demos):
        cls = d.get("classification", {})
        summary_parts = [f"#{i}: {d.get('name', '?')}"]
        if cls.get("type"):
            summary_parts.append(f"type={cls['type']}")
        if cls.get("overall_score"):
            summary_parts.append(f"score={cls['overall_score']}/10")
        if cls.get("summary"):
            summary_parts.append(cls["summary"][:150])
        if d.get("tags"):
            summary_parts.append(f"tags={','.join(d['tags'])}")
        if d.get("category"):
            summary_parts.append(f"category={d['category']}")

        # Include key step text snippets for context
        step_texts = d.get("steps_text", [])
        if step_texts:
            first_step = step_texts[0].get("text", "")[:100]
            if first_step:
                summary_parts.append(f"opens_with=\"{first_step}\"")

        demo_summaries.append(" | ".join(summary_parts))

    import anthropic
    client = anthropic.Anthropic(api_key=effective_key)
    model = get_model("haiku", effective_key)

    prompt = f"""You have a database of {len(demos)} product demos. The user wants to find specific demos.

## DEMO DATABASE
{chr(10).join(demo_summaries)}

## USER QUERY
"{query}"

Return a JSON array of demo indices (the # numbers) that best match the query, ranked by relevance. Include up to 10 results. Consider the demo name, type, score, summary, tags, category, and opening text.

If no demos match, return an empty array.

Respond with ONLY a JSON array of integers, e.g. [3, 7, 1]. No other text."""

    try:
        response = call_with_fallback(
            client, model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.content[0].text.strip()
        # Extract array
        m = re.search(r'\[[\d,\s]*\]', response_text)
        if m:
            indices = json.loads(m.group())
            return [demos[i] for i in indices if 0 <= i < len(demos)]
    except Exception as e:
        print(f"   Smart query failed: {e}")

    # Fallback to keyword search
    return search_index(index, query)


def filter_index(index: dict, type_filter: str = None, min_score: float = None,
                 scanned_only: bool = False, unscanned_only: bool = False) -> list:
    """Filter index entries."""
    results = []
    for demo in index["demos"].values():
        cls = demo.get("classification", {})

        if type_filter:
            demo_type = cls.get("type", "").lower()
            if type_filter.lower() not in demo_type:
                continue

        if min_score is not None:
            score = cls.get("overall_score", cls.get("score", 0))
            if not score or score < min_score:
                continue

        if scanned_only and not demo.get("last_scanned_at"):
            continue

        if unscanned_only and demo.get("last_scanned_at"):
            continue

        results.append(demo)

    # Sort by score descending, then by name
    results.sort(key=lambda d: (
        -(d.get("classification", {}).get("overall_score", 0) or 0),
        d.get("name", "")
    ))
    return results


def get_index_stats(index: dict) -> dict:
    """Compute statistics about the index."""
    demos = list(index["demos"].values())
    total = len(demos)
    scanned = sum(1 for d in demos if d.get("last_scanned_at"))
    unscanned = total - scanned
    accessible = sum(1 for d in demos if d.get("is_accessible", True) and not d.get("is_gated"))
    gated = sum(1 for d in demos if d.get("is_gated"))

    # Type breakdown
    type_counts = {}
    scores = []
    for d in demos:
        cls = d.get("classification", {})
        t = cls.get("type", "unclassified")
        type_counts[t] = type_counts.get(t, 0) + 1
        score = cls.get("overall_score", cls.get("score"))
        if score:
            scores.append(score)

    # Source breakdown
    source_counts = {}
    for d in demos:
        src = d.get("source", "showcase")
        source_counts[src] = source_counts.get(src, 0) + 1

    avg_score = sum(scores) / len(scores) if scores else 0

    # Scan depth breakdown
    light_scan = sum(
        1 for d in demos
        if d.get("last_scanned_at")
        and not d.get("screenshots_captured")
    )
    full_scan = sum(
        1 for d in demos
        if d.get("last_scanned_at")
        and d.get("screenshots_captured")
    )

    return {
        "total_demos": total,
        "scanned": scanned,
        "unscanned": unscanned,
        "light_scan": light_scan,
        "full_scan": full_scan,
        "accessible": accessible,
        "gated": gated,
        "type_breakdown": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
        "source_breakdown": source_counts,
        "avg_score": round(avg_score, 1),
        "highest_score": max(scores) if scores else 0,
        "lowest_score": min(scores) if scores else 0,
        "last_sync": index.get("last_sync"),
    }


# ---------------------------------------------------------------------------
# Data classes (for Playwright walking)
# ---------------------------------------------------------------------------

@dataclass
class DemoInfo:
    name: str
    showcase_url: str
    demo_iframe_url: str = ""
    demo_domain: str = ""
    live_preview_url: str = ""
    category: str = ""
    is_accessible: bool = True
    is_gated: bool = False
    error: str = ""

@dataclass
class DemoStep:
    step_number: int
    total_steps: int
    tooltip_text: str = ""
    screenshot_path: str = ""
    has_hotspot: bool = False
    has_next_button: bool = False

@dataclass
class DemoResult:
    info: DemoInfo
    steps: list = field(default_factory=list)
    total_steps_found: int = 0
    steps_captured: int = 0
    classification: dict = field(default_factory=dict)
    has_chapters: bool = False
    chapter_count: int = 0

    def to_index_entry(self, source: str = "showcase") -> dict:
        """Convert to a dict suitable for merging into the index."""
        return {
            "name": self.info.name,
            "showcase_url": self.info.showcase_url,
            "demo_url": self.info.demo_iframe_url,
            "live_preview_url": self.info.live_preview_url,
            "category": self.info.category,
            "is_accessible": self.info.is_accessible,
            "is_gated": self.info.is_gated,
            "error": self.info.error,
            "total_steps": self.total_steps_found,
            "steps_captured": self.steps_captured,
            "steps_text": [
                {"step": s.step_number, "total": s.total_steps, "text": s.tooltip_text}
                for s in self.steps
            ],
            "classification": self.classification,
            "source": source,
            "screenshots_captured": self.steps_captured > 0,
            "has_chapters": self.has_chapters,
            "chapter_count": self.chapter_count,
        }


# ---------------------------------------------------------------------------
# STEP 1: Scrape showcase page for all demo URLs
# ---------------------------------------------------------------------------

async def scrape_showcase(page) -> list[DemoInfo]:
    print("\n--- STEP 1: Scraping showcase page ---")
    print(f"   Navigating to {SHOWCASE_URL}")

    await page.goto(SHOWCASE_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
    await page.wait_for_timeout(2000)

    demos = await page.evaluate("""() => {
        const links = document.querySelectorAll('a[href*="/customer-showcase/"]');
        const seen = new Set();
        const results = [];
        links.forEach(a => {
            const href = a.href;
            if (seen.has(href) || href === window.location.href) return;
            seen.add(href);
            const card = a.closest('[class*="card"], [class*="Card"], div') || a;
            const nameEl = card.querySelector('h2, h3, h4, [class*="name"], [class*="Name"]');
            const name = nameEl ? nameEl.textContent.trim() : href.split('/').pop();
            const catEl = card.querySelector('[class*="category"], [class*="tag"], [class*="industry"]');
            const category = catEl ? catEl.textContent.trim() : '';
            results.push({ name, showcase_url: href, category });
        });
        return results;
    }""")

    demo_list = []
    for d in demos:
        demo_list.append(DemoInfo(
            name=d["name"],
            showcase_url=d["showcase_url"],
            category=d.get("category", "")
        ))

    print(f"   Found {len(demo_list)} demos on showcase page")
    return demo_list


# ---------------------------------------------------------------------------
# STEP 2: Extract demo iframe URL from showcase page
# ---------------------------------------------------------------------------

async def extract_demo_url(page, demo: DemoInfo) -> DemoInfo:
    try:
        await page.goto(demo.showcase_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        await page.wait_for_timeout(2000)

        result = await page.evaluate("""() => {
            const iframes = document.querySelectorAll('iframe');
            let demoUrl = '';
            let demoDomain = '';
            for (const iframe of iframes) {
                try {
                    const url = new URL(iframe.src);
                    if (url.pathname.includes('/demo/')) {
                        demoUrl = iframe.src;
                        demoDomain = url.hostname;
                        break;
                    }
                } catch(e) {}
            }
            let livePreviewUrl = '';
            const links = document.querySelectorAll('a');
            for (const a of links) {
                if (a.textContent.includes('View live')) {
                    livePreviewUrl = a.href;
                    break;
                }
            }
            const forms = document.querySelectorAll('form, [class*="gate"], [class*="Gate"], [class*="leadCapture"]');
            const isGated = forms.length > 0;
            return { demoUrl, demoDomain, livePreviewUrl, isGated };
        }""")

        demo.demo_iframe_url = result["demoUrl"]
        demo.demo_domain = result["demoDomain"]
        demo.live_preview_url = result["livePreviewUrl"]
        demo.is_gated = result["isGated"]

        if not demo.demo_iframe_url:
            demo.is_accessible = False
            demo.error = "No demo iframe found on showcase page"

    except Exception as e:
        demo.is_accessible = False
        demo.error = f"Failed to load showcase page: {str(e)[:100]}"

    return demo


# ---------------------------------------------------------------------------
# STEP 3: Walk through a demo
# ---------------------------------------------------------------------------

async def walk_demo(page, demo: DemoInfo, demo_index: int) -> DemoResult:
    result = DemoResult(info=demo)

    if not demo.demo_iframe_url:
        print(f"   Skipping {demo.name} -- no demo URL found")
        return result

    demo_dir = SCREENSHOTS_DIR / _safe_filename(demo.name)
    demo_dir.mkdir(parents=True, exist_ok=True)

    try:
        print(f"   Loading demo: {demo.demo_iframe_url[:80]}...")
        await page.goto(demo.demo_iframe_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        await page.wait_for_timeout(3000)

        demo_check = await page.evaluate("""() => {
            const hasPlayer = document.querySelector('[data-testid="demoplayer-image"], [class*="DemoPlayer"], [class*="Widget"]') !== null;
            const hasNextBtn = document.querySelector('[data-testid="widget-cta"]') !== null;
            const hasHotspot = document.querySelector('[class*="Hotspot"], [class*="hotspot"]') !== null;
            const hasTooltip = document.querySelector('[class*="Tooltip"], [class*="tooltip"]') !== null;
            const hasFlowlist = document.querySelector('[data-testid="flowlist"]') !== null;
            const isInteractive = hasNextBtn || hasHotspot || hasTooltip || hasFlowlist;
            return { hasPlayer, isInteractive };
        }""")

        if not demo_check["hasPlayer"] or not demo_check["isInteractive"]:
            has_form = await page.evaluate("""() => {
                return document.querySelector('form, input[type="email"], [class*="leadCapture"], [class*="gate"]') !== null;
            }""")
            if has_form:
                demo.is_gated = True
                demo.error = "Demo is gated (requires form submission)"
                print(f"   Gated -- skipping")
                return result
            elif demo_check["hasPlayer"] and not demo_check["isInteractive"]:
                demo.is_accessible = False
                demo.error = "Not an interactive demo (static image/GIF/video only)"
                print(f"   Static content (not interactive) -- skipping")
                return result
            else:
                demo.is_accessible = False
                demo.error = "Demo did not load -- no player elements found"
                print(f"   Demo did not load")
                return result

        # Check for flowlist (chapter hub) pattern before entering the main step loop
        flowlist_chapters = await page.evaluate("""() => {
            const flowlist = document.querySelector('[data-testid="flowlist"]');
            if (!flowlist) return null;
            const items = Array.from(document.querySelectorAll('[data-testid^="flowlist-item-"]'));
            return items.map((el, i) => ({
                index: i,
                testId: el.getAttribute('data-testid'),
                text: el.textContent.trim().substring(0, 100)
            }));
        }""")

        if flowlist_chapters:
            result.has_chapters = True
            result.chapter_count = len(flowlist_chapters)
            print(f"   Flowlist detected: {len(flowlist_chapters)} chapters")
            hub_screenshot = demo_dir / f"step_000_hub.png"
            await page.screenshot(path=str(hub_screenshot), full_page=False)
            chapter_names = ", ".join(c["text"][:30] for c in flowlist_chapters)
            hub_step = DemoStep(
                step_number=0,
                total_steps=len(flowlist_chapters),
                tooltip_text=f"Chapter hub with {len(flowlist_chapters)} chapters: {chapter_names}",
                screenshot_path=str(hub_screenshot),
                has_hotspot=False,
                has_next_button=False,
            )
            result.steps.append(hub_step)
            result.total_steps_found = len(flowlist_chapters)

            for chapter in flowlist_chapters:
                if len(result.steps) >= MAX_STEPS_PER_DEMO:
                    break

                print(f"   Walking chapter {chapter['index']+1}/{len(flowlist_chapters)}: {chapter['text'][:50]}")
                try:
                    chapter_btn = page.locator(f'[data-testid="{chapter["testId"]}"]')
                    await chapter_btn.click(timeout=STEP_TIMEOUT_MS)
                    await page.wait_for_timeout(STEP_TRANSITION_WAIT_MS)
                except Exception as e:
                    print(f"      Could not click chapter {chapter['index']+1}: {e}")
                    continue

                # Walk this chapter's linear steps using the same logic as the main loop
                chapter_step_num = 0
                while len(result.steps) < MAX_STEPS_PER_DEMO:
                    chapter_step_num += 1

                    step_info = await page.evaluate("""() => {
                        const tooltip = document.querySelector(
                            '[class*="TooltipPositionManager"], [class*="WidgetManager"], [class*="ModalWidget"]'
                        );
                        const tooltipText = tooltip ? tooltip.textContent.trim() : '';
                        const pageMatch = tooltipText.match(/(\\d+)\\/(\\d+)/);
                        const currentStep = pageMatch ? parseInt(pageMatch[1]) : 0;
                        const totalSteps = pageMatch ? parseInt(pageMatch[2]) : 0;
                        const nextBtn = document.querySelector('[data-testid="widget-cta"]');
                        const hasNext = nextBtn !== null && nextBtn.offsetParent !== null;
                        const nextBtnText = nextBtn ? nextBtn.textContent.trim() : '';
                        const hotspot = document.querySelector(
                            '[class*="HotspotLegacy_beaconClickableArea"], [class*="WidgetHotspotBeacon"]'
                        );
                        const hasHotspot = hotspot !== null;
                        const hasForm = document.querySelector(
                            'form, input[type="email"], [class*="leadCapture"]'
                        ) !== null;
                        return {
                            tooltipText: tooltipText.substring(0, 500),
                            currentStep, totalSteps,
                            hasNext, nextBtnText,
                            hasHotspot, hasForm
                        };
                    }""")

                    if step_info["hasForm"]:
                        print(f"      Hit a gated form at step {chapter_step_num} -- stopping")
                        demo.is_gated = True
                        break

                    total = step_info["totalSteps"] or chapter_step_num
                    current = step_info["currentStep"] or chapter_step_num

                    screenshot_path = demo_dir / f"step_{len(result.steps):03d}.png"
                    await page.screenshot(path=str(screenshot_path), full_page=False)

                    step = DemoStep(
                        step_number=current,
                        total_steps=total,
                        tooltip_text=step_info["tooltipText"],
                        screenshot_path=str(screenshot_path),
                        has_hotspot=step_info["hasHotspot"],
                        has_next_button=step_info["hasNext"],
                    )
                    result.steps.append(step)
                    result.total_steps_found = max(result.total_steps_found, total)
                    result.steps_captured = len(result.steps)

                    print(f"      Step {current}/{total}: {step_info['tooltipText'][:60]}...")

                    if step_info["hasNext"]:
                        try:
                            btn = page.locator('[data-testid="widget-cta"]')
                            await btn.click(timeout=STEP_TIMEOUT_MS)
                            await page.wait_for_timeout(STEP_TRANSITION_WAIT_MS)
                        except Exception:
                            try:
                                hotspot = page.locator('[class*="HotspotLegacy_beaconClickableArea"]').first
                                await hotspot.click(timeout=3000)
                                await page.wait_for_timeout(STEP_TRANSITION_WAIT_MS)
                            except Exception:
                                print(f"      Could not advance past step {chapter_step_num}")
                                break
                    elif step_info["hasHotspot"]:
                        try:
                            hotspot = page.locator('[class*="HotspotLegacy_beaconClickableArea"]').first
                            await hotspot.click(timeout=STEP_TIMEOUT_MS)
                            await page.wait_for_timeout(STEP_TRANSITION_WAIT_MS)
                        except Exception:
                            print(f"      Could not click hotspot at step {chapter_step_num}")
                            break
                    else:
                        print(f"      End of chapter {chapter['index']+1} at step {chapter_step_num}")
                        break

                    if step_info["totalSteps"] > 0 and current >= step_info["totalSteps"]:
                        print(f"      Completed all {total} steps in chapter {chapter['index']+1}")
                        break

                # Navigate back to the flowlist for the next chapter
                if chapter["index"] < len(flowlist_chapters) - 1 and len(result.steps) < MAX_STEPS_PER_DEMO:
                    await page.goto(demo.demo_iframe_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
                    await page.wait_for_timeout(3000)

        else:
            step_num = 0
            while step_num < MAX_STEPS_PER_DEMO:
                step_num += 1

                step_info = await page.evaluate("""() => {
                    const tooltip = document.querySelector(
                        '[class*="TooltipPositionManager"], [class*="WidgetManager"], [class*="ModalWidget"]'
                    );
                    const tooltipText = tooltip ? tooltip.textContent.trim() : '';
                    const pageMatch = tooltipText.match(/(\\d+)\\/(\\d+)/);
                    const currentStep = pageMatch ? parseInt(pageMatch[1]) : 0;
                    const totalSteps = pageMatch ? parseInt(pageMatch[2]) : 0;
                    const nextBtn = document.querySelector('[data-testid="widget-cta"]');
                    const hasNext = nextBtn !== null && nextBtn.offsetParent !== null;
                    const nextBtnText = nextBtn ? nextBtn.textContent.trim() : '';
                    const hotspot = document.querySelector(
                        '[class*="HotspotLegacy_beaconClickableArea"], [class*="WidgetHotspotBeacon"]'
                    );
                    const hasHotspot = hotspot !== null;
                    const hasForm = document.querySelector(
                        'form, input[type="email"], [class*="leadCapture"]'
                    ) !== null;
                    return {
                        tooltipText: tooltipText.substring(0, 500),
                        currentStep, totalSteps,
                        hasNext, nextBtnText,
                        hasHotspot, hasForm
                    };
                }""")

                if step_info["hasForm"]:
                    print(f"   Hit a gated form at step {step_num} -- stopping")
                    demo.is_gated = True
                    break

                total = step_info["totalSteps"] or step_num
                current = step_info["currentStep"] or step_num

                screenshot_path = demo_dir / f"step_{step_num:03d}.png"
                await page.screenshot(path=str(screenshot_path), full_page=False)

                step = DemoStep(
                    step_number=current,
                    total_steps=total,
                    tooltip_text=step_info["tooltipText"],
                    screenshot_path=str(screenshot_path),
                    has_hotspot=step_info["hasHotspot"],
                    has_next_button=step_info["hasNext"],
                )
                result.steps.append(step)
                result.total_steps_found = total
                result.steps_captured = len(result.steps)

                print(f"      Step {current}/{total}: {step_info['tooltipText'][:60]}...")

                if step_info["hasNext"]:
                    try:
                        btn = page.locator('[data-testid="widget-cta"]')
                        await btn.click(timeout=STEP_TIMEOUT_MS)
                        await page.wait_for_timeout(STEP_TRANSITION_WAIT_MS)
                    except Exception:
                        try:
                            hotspot = page.locator('[class*="HotspotLegacy_beaconClickableArea"]').first
                            await hotspot.click(timeout=3000)
                            await page.wait_for_timeout(STEP_TRANSITION_WAIT_MS)
                        except Exception:
                            print(f"      Could not advance past step {step_num}")
                            break
                elif step_info["hasHotspot"]:
                    try:
                        hotspot = page.locator('[class*="HotspotLegacy_beaconClickableArea"]').first
                        await hotspot.click(timeout=STEP_TIMEOUT_MS)
                        await page.wait_for_timeout(STEP_TRANSITION_WAIT_MS)
                    except Exception:
                        print(f"      Could not click hotspot at step {step_num}")
                        break
                else:
                    print(f"      Reached end of demo at step {step_num}")
                    break

                if step_info["totalSteps"] > 0 and current >= step_info["totalSteps"]:
                    print(f"      Completed all {total} steps")
                    break

    except Exception as e:
        demo.error = f"Error walking demo: {str(e)[:200]}"
        print(f"   Error: {demo.error}")

    return result


# ---------------------------------------------------------------------------
# STEP 4: Classify demos using Claude API
# ---------------------------------------------------------------------------

def generate_rubric_from_doc(doc_text: str, output_path: Path = None, api_key: str = None) -> str:
    import anthropic

    effective_key = api_key or ANTHROPIC_API_KEY
    if not effective_key:
        raise ValueError("No API key provided -- cannot generate rubric")

    print("   Generating classification rubric from uploaded document (using Sonnet)...")

    prompt = f"""You are an expert at creating evaluation rubrics for interactive product demos.

The user has provided a framework document that describes how they want demos to be evaluated. Your job is to convert this into a clean, structured classification rubric that an AI can use to consistently evaluate product demos.

## Your output MUST include:

1. **A brief intro** (1-2 sentences) explaining what is being evaluated and the core framework.

2. **Classification Buckets** -- Extract the key categories/types from the document. Each bucket should have:
   - A clear name
   - A description of what makes a demo fall into this bucket
   - Specific signals/indicators to look for (bullet points)
   - Aim for 4-7 buckets total. Always include a "Gated / Inaccessible" bucket at the end.

3. **Evaluation Dimensions** -- Extract the scoring dimensions. Each should be:
   - A snake_case name (e.g., logic_score, emotion_score)
   - A clear description of what 1 vs 10 means
   - Aim for 4-8 dimensions.

4. **Output Format** -- Always end with this exact JSON schema:
```
## Output Format

Respond in JSON:
{{
  "type": "one of the classification buckets above",
  "overall_score": 1-10,
  [one key per evaluation dimension, e.g. "logic_score": 1-10],
  "summary": "2-3 sentence summary of what the demo shows and how it tells its story",
  "strengths": ["list of specific things done well"],
  "weaknesses": ["list of specific areas for improvement"],
  "recommendation": "One specific actionable suggestion to improve this demo"
}}
```

## Rules:
- Be faithful to the user's framework -- don't invent categories they didn't describe
- If the document is vague in some areas, make reasonable inferences but stay true to the intent
- Use clear, concrete language.
- The rubric should be self-contained.

## The user's framework document:

{doc_text}
"""

    client = anthropic.Anthropic(api_key=effective_key)
    model_id = get_model("sonnet", effective_key)
    print(f"   Using model: {model_id}")
    response = call_with_fallback(
        client, model_id,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )

    rubric = response.content[0].text.strip()
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rubric)
        print(f"   Rubric saved to {output_path}")

    return rubric


def load_classification_criteria(custom_rubric_path: str = None) -> str:
    if custom_rubric_path:
        p = Path(custom_rubric_path)
        if p.exists():
            criteria = p.read_text().strip()
            if criteria:
                print(f"   Loaded custom rubric from {p.name}")
                return criteria

    if CLASSIFICATION_CRITERIA_FILE.exists():
        criteria = CLASSIFICATION_CRITERIA_FILE.read_text().strip()
        if criteria:
            return criteria

    return """
Classify this interactive product demo into one of the following categories based on the screenshots and tooltip text from each step:

## Demo Types
1. **Storytelling Demo (Good)**: Has a clear narrative arc. Starts with context/problem, walks through the solution step-by-step, and ends with value/outcome. Tooltip text guides the viewer with explanatory copy. Steps flow logically.
2. **Storytelling Demo (Needs Improvement)**: Attempts storytelling but has gaps.
3. **Feature Walkthrough**: A straightforward tour of product features without a narrative arc.
4. **Click-Through Demo**: Minimal guidance, just hotspots to click.
5. **Gated/Inaccessible**: Demo requires form submission or is otherwise not fully accessible.

## Evaluation Criteria
- **Narrative flow**: Does the demo tell a story from problem to solution?
- **Copy quality**: Are tooltips informative, concise, and guiding?
- **Step progression**: Do steps build on each other logically?
- **Visual quality**: Are screenshots clean, focused, and well-composed?
- **Length**: Is the demo an appropriate length?
- **Call to action**: Does it end with a clear next step?
"""


def _extract_json(text: str) -> Optional[dict]:
    """Robustly extract JSON from a model response. Handles markdown fences, preamble, etc."""
    # Try markdown json fence first
    m = re.search(r'```json\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Try any markdown fence
    m = re.search(r'```\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Try finding JSON object directly (first { to last })
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass
    # Last resort: try the whole thing
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


def _validate_classification(cls: dict) -> dict:
    """Ensure classification has required fields with correct types."""
    # Ensure type exists
    if "type" not in cls or not cls["type"]:
        cls["type"] = "unclassified"

    # Ensure scores are numbers 1-10
    for key in ["overall_score", "logic_score", "emotion_score", "credibility_score",
                "narrative_flow_score", "copy_quality_score"]:
        if key in cls:
            try:
                val = int(cls[key])
                cls[key] = max(1, min(10, val))
            except (ValueError, TypeError):
                cls.pop(key, None)

    # Ensure list fields are lists
    for key in ["strengths", "weaknesses"]:
        if key in cls and not isinstance(cls[key], list):
            cls[key] = [cls[key]] if cls[key] else []

    # Generate auto-tags from classification for searchability
    tags = []
    demo_type = cls.get("type", "").lower()
    if "strong" in demo_type or "good" in demo_type:
        tags.append("strong-demo")
    if "feature dump" in demo_type or "dump" in demo_type:
        tags.append("feature-dump")
    if "generic" in demo_type or "persona" in demo_type:
        tags.append("weak-targeting")
    if "claim" in demo_type or "proof" in demo_type:
        tags.append("needs-proof")
    if "click" in demo_type or "minimal" in demo_type:
        tags.append("minimal-guidance")
    if "gated" in demo_type:
        tags.append("gated")

    overall = cls.get("overall_score", 0)
    if overall and overall >= 7:
        tags.append("high-quality")
    elif overall and overall <= 3:
        tags.append("low-quality")

    cls["_auto_tags"] = tags

    return cls


async def classify_demo(demo_result: DemoResult, mode: str = "fast", criteria_file: str = None, api_key: str = None) -> dict:
    import anthropic

    effective_key = api_key or ANTHROPIC_API_KEY
    if not effective_key:
        return {"error": "No API key configured", "type": "unclassified"}

    if not demo_result.steps:
        return {"type": "inaccessible", "reason": demo_result.info.error or "No steps captured"}

    criteria = load_classification_criteria(criteria_file)

    use_screenshots = (mode == "full")
    tier = "haiku" if mode in ("fast", "smart") else "sonnet"
    model = get_model(tier, effective_key)
    model_label = "Haiku" if "haiku" in model else "Sonnet"

    # Build step data
    steps_to_send = demo_result.steps
    if use_screenshots and len(steps_to_send) > 15:
        indices = [0]
        step_size = (len(steps_to_send) - 1) / 14
        for i in range(1, 14):
            indices.append(round(i * step_size))
        indices.append(len(steps_to_send) - 1)
        indices = sorted(set(indices))
        steps_to_send = [demo_result.steps[i] for i in indices]

    step_texts = []
    for step in steps_to_send:
        step_texts.append(f"Step {step.step_number}/{step.total_steps}: {step.tooltip_text}")

    # Build message content
    content = []

    # Load best practices from knowledge base for richer analysis
    kb = load_knowledge_base()
    best_practices_section = ""
    if kb.get("tips"):
        tips_by_cat = {}
        for tip in kb["tips"]:
            cat = tip.get("category", "general")
            tips_by_cat.setdefault(cat, []).append(tip["tip"])
        bp_lines = []
        for cat, cat_tips in tips_by_cat.items():
            bp_lines.append(f"**{cat}**: " + "; ".join(cat_tips[:4]))
        best_practices_section = "\n\n## BEST PRACTICES REFERENCE\n\nWhen analyzing, consider whether the demo follows these proven best practices:\n" + "\n".join(f"- {line}" for line in bp_lines)

    # System-like instructions at the top for reliable structured output
    system_instruction = f"""You are a demo storytelling analyst. You will analyze an interactive product demo and return a JSON classification.

COMPANY: {demo_result.info.name}
TOTAL STEPS: {len(demo_result.steps)}

## CLASSIFICATION RUBRIC

{criteria}{best_practices_section}

## DEMO CONTENT

"""
    content.append({"type": "text", "text": system_instruction})

    if use_screenshots:
        for step in steps_to_send:
            content.append({"type": "text", "text": f"\n{step.step_number}/{step.total_steps}: {step.tooltip_text}\n"})
            screenshot_path = Path(step.screenshot_path)
            if screenshot_path.exists():
                img_data = base64.standard_b64encode(screenshot_path.read_bytes()).decode("utf-8")
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img_data}
                })
    else:
        content.append({"type": "text", "text": "\n".join(step_texts)})

    # Strong closing instruction for JSON output
    content.append({"type": "text", "text": """

## YOUR TASK

Classify this demo according to the rubric above. You MUST respond with ONLY a single JSON object (no markdown fences, no explanation before or after). The JSON must include at minimum:
- "type": string (one of the classification buckets from the rubric)
- "overall_score": integer 1-10
- "summary": string (2-3 sentences)
- "strengths": array of strings
- "weaknesses": array of strings
- "recommendation": string

Include any additional scoring dimensions specified in the rubric (e.g. logic_score, emotion_score, credibility_score, narrative_flow_score, copy_quality_score).

IMPORTANT: Output ONLY the JSON object. No other text."""})

    # Attempt classification with retry on parse failure
    max_attempts = 2
    last_error = None

    for attempt in range(max_attempts):
        try:
            client = anthropic.Anthropic(api_key=effective_key)
            response = call_with_fallback(
                client, model,
                max_tokens=2000,
                messages=[{"role": "user", "content": content}],
            )

            response_text = response.content[0].text.strip()
            classification = _extract_json(response_text)

            if classification is None:
                last_error = f"Failed to parse JSON from response (attempt {attempt+1})"
                if attempt < max_attempts - 1:
                    # Retry with even stronger instruction
                    content[-1] = {"type": "text", "text": """

Respond with ONLY a raw JSON object. Do NOT use markdown code fences. Do NOT include any text before or after the JSON. Start your response with { and end with }.

Required fields: type (string), overall_score (integer 1-10), summary (string), strengths (array), weaknesses (array), recommendation (string)."""}
                    continue

                return {"type": "unclassified", "error": last_error, "raw_response": response_text[:500], "_model": model_label}

            classification = _validate_classification(classification)
            classification["_model"] = model_label
            classification["_mode"] = mode
            return classification

        except Exception as e:
            last_error = str(e)[:200]
            if attempt < max_attempts - 1:
                continue
            return {"error": last_error, "type": "unclassified", "_model": model_label}


# ---------------------------------------------------------------------------
# Report generation (also exports from index)
# ---------------------------------------------------------------------------

def generate_report(index: dict):
    """Generate CSV and JSON reports from the current index."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    demos = list(index["demos"].values())

    # JSON report
    json_path = OUTPUT_DIR / "demo_report.json"
    with open(json_path, "w") as f:
        json.dump(demos, f, indent=2)

    # CSV summary
    csv_path = OUTPUT_DIR / "demo_report.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Name", "Category", "Demo URL", "Showcase URL", "Accessible", "Gated",
            "Total Steps", "Steps Captured", "Classification Type",
            "Overall Score", "Logic Score", "Emotion Score", "Credibility Score",
            "Narrative Flow Score", "Copy Quality Score",
            "Summary", "Strengths", "Weaknesses",
            "Recommendation", "Source", "Discovered", "Last Scanned", "Scan Count", "Tags"
        ])
        for d in demos:
            cls = d.get("classification", {})
            writer.writerow([
                d.get("name", ""),
                d.get("category", ""),
                d.get("demo_url", ""),
                d.get("showcase_url", ""),
                d.get("is_accessible", ""),
                d.get("is_gated", ""),
                d.get("total_steps", ""),
                d.get("steps_captured", ""),
                cls.get("type", ""),
                cls.get("overall_score", cls.get("score", "")),
                cls.get("logic_score", ""),
                cls.get("emotion_score", ""),
                cls.get("credibility_score", ""),
                cls.get("narrative_flow_score", ""),
                cls.get("copy_quality_score", ""),
                cls.get("summary", ""),
                "; ".join(cls.get("strengths", [])) if isinstance(cls.get("strengths"), list) else cls.get("strengths", ""),
                "; ".join(cls.get("weaknesses", [])) if isinstance(cls.get("weaknesses"), list) else cls.get("weaknesses", ""),
                cls.get("recommendation", ""),
                d.get("source", ""),
                d.get("discovered_at", ""),
                d.get("last_scanned_at", ""),
                d.get("scan_count", 0),
                "; ".join(d.get("tags", [])),
            ])


def print_demo_table(demos: list, title: str = "Demos"):
    """Print a formatted table of demos to stdout."""
    if not demos:
        print(f"\n   No demos found.")
        return

    print(f"\n   {title} ({len(demos)} results)")
    print(f"   {'─' * 90}")
    print(f"   {'Name':<35} {'Type':<25} {'Score':>5}  {'Steps':>5}  {'Scanned':<12}")
    print(f"   {'─' * 90}")

    for d in demos:
        cls = d.get("classification", {})
        name = d.get("name", "Unknown")[:34]
        demo_type = cls.get("type", "unscanned")[:24]
        score = cls.get("overall_score", cls.get("score", ""))
        score_str = f"{score}/10" if score else "  -- "
        steps = d.get("steps_captured", 0)
        steps_str = str(steps) if steps else "  -- "
        scanned = d.get("last_scanned_at", "")
        scanned_str = scanned[:10] if scanned else "never"

        print(f"   {name:<35} {demo_type:<25} {score_str:>5}  {steps_str:>5}  {scanned_str:<12}")

    print(f"   {'─' * 90}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_ " else "" for c in name).strip().replace(" ", "_")[:50]


def import_urls_from_file(filepath: str) -> list[str]:
    """Read URLs from a file (one per line, or comma-separated)."""
    p = Path(filepath)
    if not p.exists():
        print(f"   Error: File not found: {filepath}")
        return []

    text = p.read_text().strip()
    urls = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Handle comma-separated on same line
        for part in line.split(","):
            part = part.strip()
            if part.startswith("http"):
                urls.append(part)
    return urls


def import_urls_from_string(url_string: str) -> list[str]:
    """Parse URLs from a comma/newline-separated string."""
    urls = []
    for part in url_string.replace("\n", ",").split(","):
        part = part.strip()
        if part.startswith("http"):
            urls.append(part)
    return urls


# ---------------------------------------------------------------------------
# Knowledge Base: Tips & Tricks + Automated Suggestions
# ---------------------------------------------------------------------------

KNOWLEDGE_BASE_FILE = Path(__file__).parent / "demo_knowledge_base.json"

_knowledge_cache = None


def load_knowledge_base() -> dict:
    """Load the tips & tricks knowledge base."""
    global _knowledge_cache
    if _knowledge_cache is not None:
        return _knowledge_cache
    if KNOWLEDGE_BASE_FILE.exists():
        try:
            with open(KNOWLEDGE_BASE_FILE) as f:
                _knowledge_cache = json.load(f)
                return _knowledge_cache
        except (json.JSONDecodeError, IOError):
            pass
    _knowledge_cache = {"tips": [], "suggestions": [], "tip_categories": []}
    return _knowledge_cache


def generate_automated_suggestions(demo: dict) -> list[dict]:
    """
    Generate rule-based suggestions for a demo based on the Demo Suggestions spreadsheet.
    Returns a list of {category, logic, suggestion, severity} dicts.
    """
    suggestions = []
    cls = demo.get("classification", {})
    steps = demo.get("steps_captured", 0) or demo.get("total_steps", 0)
    steps_text = demo.get("steps_text", [])

    # --- Demo length rules ---
    if steps and steps < 6:
        suggestions.append({
            "category": "Demo length",
            "logic": f"Too few steps: {steps} steps",
            "suggestion": "This demo has fewer than 6 steps. Add a couple more steps to tell a complete story.",
            "severity": "medium",
        })
    if steps and steps > 15:
        suggestions.append({
            "category": "Demo length",
            "logic": f"Too many steps: {steps} steps",
            "suggestion": "With 15+ steps, this demo is getting long. Try cutting down to 12 steps to keep viewers from dropping off.",
            "severity": "medium",
        })
    if steps and steps > 20:
        suggestions.append({
            "category": "Demo length",
            "logic": f"Very long: {steps} steps",
            "suggestion": "This demo has more than 20 steps. Consider breaking it into chapters or Buyer Hubs.",
            "severity": "high",
        })

    # --- Guide content: long copy ---
    long_copy_count = 0
    for st in steps_text:
        text = st.get("text", "") if isinstance(st, dict) else str(st)
        if len(text) > 200:
            long_copy_count += 1
    if long_copy_count > 0:
        suggestions.append({
            "category": "Guide content",
            "logic": f"{long_copy_count} steps with >200 characters",
            "suggestion": "Some guides are running long. Aim for under 200 characters so viewers actually read through them.",
            "severity": "low",
        })

    # --- Missing CTA (from classification) ---
    weaknesses = cls.get("weaknesses", [])
    if isinstance(weaknesses, str):
        weaknesses = [weaknesses]
    weakness_text = " ".join(w.lower() for w in weaknesses)
    summary = (cls.get("summary", "") or "").lower()
    strengths = cls.get("strengths", [])
    if isinstance(strengths, str):
        strengths = [strengths]
    strength_text = " ".join(s.lower() for s in strengths)

    if "cta" not in strength_text and "call to action" not in strength_text:
        # Check steps text for CTA indicators
        all_text = " ".join(
            (st.get("text", "") if isinstance(st, dict) else str(st)).lower()
            for st in steps_text
        )
        cta_signals = ["book a demo", "get started", "start free", "sign up", "contact",
                       "schedule", "request", "try it", "learn more", "talk to"]
        has_cta = any(sig in all_text for sig in cta_signals)
        if not has_cta and steps > 0:
            suggestions.append({
                "category": "In-demo CTA",
                "logic": "No convert CTA detected",
                "suggestion": "This demo has no clear call to action. Consider adding a convert CTA button ('Book a demo', 'Start for free') to capture interest.",
                "severity": "high",
            })

    # --- Missing welcome screen / media modal ---
    if steps_text:
        first_step = steps_text[0]
        first_text = (first_step.get("text", "") if isinstance(first_step, dict) else str(first_step)).lower()
        if len(first_text) < 20 and steps > 3:
            suggestions.append({
                "category": "Welcome screen",
                "logic": "Step 1 has minimal content",
                "suggestion": "Start strong! Add a welcome screen with a video, GIF, or image to set the stage for what's coming.",
                "severity": "medium",
            })

    # --- Weak scores → targeted tips ---
    logic_score = cls.get("logic_score", 0) or 0
    emotion_score = cls.get("emotion_score", 0) or 0
    credibility_score = cls.get("credibility_score", 0) or 0
    narrative_score = cls.get("narrative_flow_score", 0) or 0
    copy_score = cls.get("copy_quality_score", 0) or 0

    if logic_score and logic_score <= 4:
        suggestions.append({
            "category": "Storytelling",
            "logic": f"Low logic score ({logic_score}/10)",
            "suggestion": "Features don't tie to a clear outcome. Title demo chapters based on use cases or outcomes, not just features. Aim for 3-5 features max, each connected to one value promise.",
            "severity": "high",
        })
    if emotion_score and emotion_score <= 4:
        suggestions.append({
            "category": "Personalization",
            "logic": f"Low emotion score ({emotion_score}/10)",
            "suggestion": "Demo feels generic. Consider adding role-based branching, persona-targeted messaging, or dynamic tokens to personalize the experience.",
            "severity": "high",
        })
    if credibility_score and credibility_score <= 4:
        suggestions.append({
            "category": "Credibility",
            "logic": f"Low credibility score ({credibility_score}/10)",
            "suggestion": "Missing proof elements. Include expected results, customer testimonials, specific metrics, or case studies within the demo steps.",
            "severity": "high",
        })
    if narrative_score and narrative_score <= 4:
        suggestions.append({
            "category": "Narrative",
            "logic": f"Low narrative flow ({narrative_score}/10)",
            "suggestion": "Demo lacks story arc. Hook prospects with a pain point or question on the welcome screen, build through the solution, and end with celebratory step + clear next steps.",
            "severity": "high",
        })
    if copy_score and copy_score <= 4:
        suggestions.append({
            "category": "Copy quality",
            "logic": f"Low copy quality ({copy_score}/10)",
            "suggestion": "Guide copy needs work. Keep copy under 15 words per tooltip, use emojis strategically, format bold text for key value props, and expand narrative via voiceovers instead.",
            "severity": "medium",
        })

    return suggestions


def get_relevant_tips(demo: dict, max_tips: int = 5) -> list[dict]:
    """
    Find tips from the knowledge base most relevant to a demo's weaknesses.
    Returns up to max_tips tips with relevance scores.
    """
    kb = load_knowledge_base()
    if not kb.get("tips"):
        return []

    cls = demo.get("classification", {})
    demo_type = (cls.get("type", "") or "").lower()
    weaknesses = cls.get("weaknesses", [])
    if isinstance(weaknesses, str):
        weaknesses = [weaknesses]
    weakness_text = " ".join(w.lower() for w in weaknesses)
    recommendation = (cls.get("recommendation", "") or "").lower()
    suggestions = generate_automated_suggestions(demo)
    suggestion_cats = set(s["category"].lower() for s in suggestions)

    scored_tips = []
    for tip in kb["tips"]:
        score = 0
        tip_text = tip["tip"].lower()
        tip_cat = tip["category"].lower()
        tip_reasoning = tip.get("reasoning", "").lower()

        # Category alignment with suggestion categories
        if any(cat in tip_cat for cat in suggestion_cats):
            score += 3

        # Weakness alignment
        for weakness in weaknesses:
            w = weakness.lower() if isinstance(weakness, str) else ""
            words = [word for word in w.split() if len(word) > 3]
            for word in words:
                if word in tip_text or word in tip_reasoning:
                    score += 2

        # Recommendation alignment
        for word in recommendation.split():
            if len(word) > 3 and word in tip_text:
                score += 1

        # Demo type alignment
        if "feature dump" in demo_type and tip_cat in ("demo structure and ux", "guides and storytelling"):
            score += 2
        if "generic" in demo_type and tip_cat in ("personalization", "welcome screen"):
            score += 2
        if "claim" in demo_type and tip_cat in ("guides and storytelling", "lead gen and conversions"):
            score += 2
        if "click-through" in demo_type and tip_cat in ("guides and storytelling", "pattern interrupts"):
            score += 2

        if score > 0:
            scored_tips.append((score, tip))

    scored_tips.sort(key=lambda x: -x[0])
    return [t[1] for t in scored_tips[:max_tips]]


def build_display_name(demo: dict) -> str:
    """
    Build a human-friendly display name for a demo by combining:
    - The known company/brand name (from URL subdomain or existing name)
    - A short descriptor from the first step's title text or classification summary

    Returns a string like "CyberArk -- Secure Cloud Access" or the best single
    identifier if we can't find a descriptor.
    """
    import re

    raw_name = (demo.get("name", "") or "").strip()
    url = demo.get("demo_url", "") or demo.get("showcase_url", "") or ""

    # --- 1. Detect if the raw name is a slug-derived junk string ---
    # Slug-derived names are typically random-looking 12+ char alphanumeric,
    # or generic labels like "View Demo", "Imported Demo", or a URL hash title-cased
    is_slug_name = False
    if raw_name:
        nm = raw_name.lower()
        if nm in ("view demo", "imported demo", "unknown", "demo"):
            is_slug_name = True
        # Random-looking: 10+ chars of mostly lowercase letters+digits, no spaces
        cleaned = raw_name.replace(" ", "")
        if len(cleaned) >= 10 and re.match(r"^[A-Za-z0-9]+$", cleaned):
            # Check for the telltale "random lowercase + digits" pattern
            has_digit = any(c.isdigit() for c in cleaned)
            has_letter = any(c.isalpha() for c in cleaned)
            if has_digit and has_letter and cleaned.lower() not in ("storylane", "channable"):
                is_slug_name = True
        # Also flag if the lowercased name matches a URL slug segment exactly
        if url:
            slug_match = re.search(r"/(demo|share)/([a-z0-9]+)", url.lower())
            if slug_match and slug_match.group(2) == cleaned.lower():
                is_slug_name = True

    # --- 2. Extract company from URL subdomain or path ---
    company = None
    if url:
        m = re.match(r"https?://([^/]+)", url)
        if m:
            host = m.group(1).lower()
            # e.g. "codesignal.storylane.io" -> "codesignal"
            # e.g. "demo.sproutsocial.com" -> "sproutsocial"
            # e.g. "democenter.channable.com" -> "channable"
            # e.g. "tour.huntress.com" -> "huntress"
            # e.g. "app.storylane.io" -> None (generic)
            parts = host.split(".")
            if len(parts) >= 3:
                first = parts[0]
                second = parts[1]
                generic_prefixes = {"app", "demo", "www", "tour", "share", "democenter",
                                    "static", "play", "watch", "view", "try", "get", "go"}
                if first in generic_prefixes:
                    # Use second-level: "sproutsocial" from "demo.sproutsocial.com"
                    if second not in ("storylane", "navattic", "walnut"):
                        company = second
                else:
                    # First subdomain is meaningful: "codesignal" from "codesignal.storylane.io"
                    company = first

            if company:
                # Title-case for display
                company = company.replace("-", " ").replace("_", " ").title()

    # --- 3. Pull descriptor from first step's text (heading/title) ---
    descriptor = None
    steps = demo.get("steps_text", [])
    if steps:
        first = steps[0]
        text = first.get("text", "") if isinstance(first, dict) else str(first)
        if text:
            # First step text often has "Title<newline or run-on>body". Try to
            # pull the leading title-ish phrase (first 8-10 words up to sentence end).
            text = text.strip()
            # Split on sentence boundaries or common heading-body seams
            parts = re.split(r"(?<=[a-z])(?=[A-Z][a-z])|[.!?]\s", text, maxsplit=1)
            lead = parts[0].strip() if parts else text[:80]
            # Trim to reasonable length
            words = lead.split()
            if len(words) > 10:
                lead = " ".join(words[:10])
            if 3 <= len(lead) <= 100:
                descriptor = lead.rstrip(":-,")

    # --- 4. Fallback descriptor from classification summary ---
    if not descriptor:
        summary = demo.get("classification", {}).get("summary", "")
        if summary:
            first_sentence = summary.split(".")[0].strip()
            words = first_sentence.split()
            if 3 <= len(words) <= 15:
                descriptor = first_sentence
            elif len(words) > 15:
                descriptor = " ".join(words[:10]) + "..."

    # --- 5. Combine ---
    # Preferred display name logic:
    # - If raw name is good (not slug-derived) → "RawName -- descriptor" OR just "RawName"
    # - Else if company detected → "Company -- descriptor" OR just "Company"
    # - Else → descriptor alone, or raw name as last resort

    base = None
    if not is_slug_name and raw_name:
        base = raw_name
    elif company:
        base = company

    if base and descriptor and descriptor.lower() != base.lower():
        # Avoid repeating company name inside descriptor
        if base.lower() not in descriptor.lower():
            return f"{base} -- {descriptor}"
        return f"{base} -- {descriptor}"
    if base:
        return base
    if descriptor:
        return descriptor

    # --- 6. Last-resort label for unscanned slug-only demos ---
    # If we hit this, we have no name, no company, no steps, no classification.
    # Surface a recognizable short ID so the user can tell them apart without
    # showing the ugly title-cased slug.
    if is_slug_name:
        # Prefer the URL slug (the last path segment) over the raw name, since
        # the raw name could be generic ("View Demo") and collide across demos.
        short = ""
        if url:
            slug_match = re.search(r"/(?:demo|share)/([a-z0-9]+)", url.lower())
            if slug_match:
                short = slug_match.group(1)[:6]
        if not short and raw_name:
            short = re.sub(r"[^A-Za-z0-9]", "", raw_name).lower()[:6]
        if short:
            return f"Unscanned Demo ({short}…)"
    return raw_name or "Unknown"


def enrich_demo_with_suggestions(demo: dict) -> dict:
    """
    Add automated suggestions, relevant tips, and a smart display_name
    to a demo entry. Mutates and returns the demo dict.
    """
    suggestions = generate_automated_suggestions(demo)
    tips = get_relevant_tips(demo)

    demo["auto_suggestions"] = suggestions
    demo["relevant_tips"] = [
        {"tip": t["tip"], "category": t["category"], "company": t.get("company", ""),
         "demo_link": t.get("demo_link", ""), "reasoning": t.get("reasoning", "")}
        for t in tips
    ]
    demo["display_name"] = build_display_name(demo)
    return demo


# ---------------------------------------------------------------------------
# Scan Layers: on-demand, query-driven deep scans
# ---------------------------------------------------------------------------

SCAN_LAYERS = {
    "social_proof": {
        "label": "Social Proof & Credibility Elements",
        "description": "Customer logos, testimonials, metrics, before/after comparisons, trust badges, case study references",
        "needs_screenshots": True,
        "tier": "haiku",
        "upgrade_to": "sonnet",
        "text_signals": ["customer", "logo", "trusted", "companies", "testimonial", "case study",
                         "results", "ROI", "saved", "increased", "reduced", "improved", "%",
                         "million", "billion", "enterprise", "fortune"],
        "prompt": """Analyze this demo step for SOCIAL PROOF and CREDIBILITY elements.

Look for:
- Customer logos or company names displayed
- Testimonial quotes or customer references
- Specific metrics/numbers (e.g. "saved 40% time", "used by 500+ companies")
- Before/after comparisons
- Trust badges, certifications, awards
- Case study references
- Realistic-looking data (vs obviously fake placeholder data)
- Industry-specific proof (compliance badges, partner logos)

Rate social_proof_score 1-10 and list every specific element found.
Respond in JSON: {"social_proof_score": N, "elements": ["list of specific items found"], "has_logos": bool, "has_metrics": bool, "has_testimonials": bool, "has_realistic_data": bool, "notes": "brief assessment"}"""
    },
    "persona_targeting": {
        "label": "Persona & Role Targeting",
        "description": "Role-specific messaging, branching paths, persona acknowledgment, stakeholder-specific language",
        "needs_screenshots": False,
        "tier": "haiku",
        "upgrade_to": None,
        "text_signals": ["role", "persona", "team", "manager", "executive", "admin", "developer",
                         "marketer", "sales", "ops", "IT", "CFO", "CTO", "VP", "director",
                         "your team", "your role", "choose", "select", "branch"],
        "prompt": """Analyze this demo for PERSONA TARGETING.

Look for:
- Role-specific language (e.g. "as a sales leader", "for your ops team")
- Branching paths where users choose their role
- Messaging tailored to specific pain points of a role
- Acknowledgment of different stakeholder priorities
- Generic vs specific language ("your business" vs "your pipeline forecast")
- Success metrics relevant to specific roles

Rate persona_score 1-10 and identify the target persona(s).
Respond in JSON: {"persona_score": N, "target_personas": ["list"], "has_branching": bool, "specificity": "generic|somewhat_targeted|highly_targeted", "role_language_examples": ["quotes from demo"], "notes": "brief assessment"}"""
    },
    "narrative_quality": {
        "label": "Narrative Arc & Story Structure",
        "description": "Problem→solution→value flow, opening hooks, story coherence, emotional journey",
        "needs_screenshots": False,
        "tier": "haiku",
        "upgrade_to": None,
        "text_signals": ["problem", "challenge", "solution", "imagine", "before", "after",
                         "result", "outcome", "journey", "discover", "transform", "unlock"],
        "prompt": """Analyze this demo's NARRATIVE STRUCTURE and STORYTELLING quality.

Look for:
- Opening hook: Does it start with a problem/challenge/question?
- Story arc: problem → solution → value/outcome?
- Transitions between steps: Do they flow logically?
- Emotional journey: Does it build tension and resolve it?
- Closing: Does it end with clear value/CTA?
- Copy quality: Informative vs generic?

Rate narrative_score 1-10 and describe the arc.
Respond in JSON: {"narrative_score": N, "has_opening_hook": bool, "has_problem_statement": bool, "has_value_outcome": bool, "has_cta": bool, "arc_summary": "problem→solution→value description", "copy_quality": "generic|functional|good|excellent", "transition_quality": "disjointed|adequate|smooth|seamless", "notes": "brief assessment"}"""
    },
    "customization": {
        "label": "Customization & Personalization",
        "description": "Dynamic content, personalized elements, custom branding, configurable features shown",
        "needs_screenshots": True,
        "tier": "haiku",
        "upgrade_to": "sonnet",
        "text_signals": ["custom", "personalize", "configure", "brand", "tailor", "adapt",
                         "your logo", "your brand", "white-label", "template", "drag and drop",
                         "flexible", "modify"],
        "prompt": """Analyze this demo for CUSTOMIZATION and PERSONALIZATION elements.

Look for:
- Personalized content (company name, logo, branding in the demo)
- Customizable UI elements being demonstrated
- Configuration/settings screens
- Template/theme selection
- White-label capabilities
- Drag-and-drop or builder interfaces
- "Make it yours" type messaging
- Before/after customization comparisons

Rate customization_score 1-10 and list elements found.
Respond in JSON: {"customization_score": N, "elements": ["list"], "shows_personalization": bool, "shows_configuration": bool, "shows_branding": bool, "notes": "brief assessment"}"""
    },
    "visual_design": {
        "label": "Visual Design & Polish",
        "description": "Screenshot quality, layout, branding consistency, visual hierarchy",
        "needs_screenshots": True,
        "tier": "haiku",
        "upgrade_to": "sonnet",
        "text_signals": [],
        "prompt": """Analyze this demo's VISUAL DESIGN quality.

Look for:
- Screenshot clarity and composition
- Consistent branding and color scheme
- Visual hierarchy and focus areas
- Use of annotations, highlights, or callouts
- Professional vs cluttered appearance
- Mobile/responsive considerations
- Tooltip design quality

Rate visual_score 1-10.
Respond in JSON: {"visual_score": N, "is_clean": bool, "has_consistent_branding": bool, "has_annotations": bool, "layout_quality": "poor|basic|good|excellent", "notes": "brief assessment"}"""
    },
    "use_case": {
        "label": "Use Case & Industry Context",
        "description": "What problem the demo solves, target industry, specific workflow demonstrated",
        "needs_screenshots": False,
        "tier": "haiku",
        "upgrade_to": None,
        "text_signals": [],
        "prompt": """Analyze what USE CASE and INDUSTRY this demo targets.

Identify:
- What specific problem/workflow does this demo show?
- What industry or vertical is it targeting?
- What job-to-be-done is being addressed?
- What product category is this? (CRM, security, analytics, HR, etc.)
- What's the key value proposition being demonstrated?

Respond in JSON: {"primary_use_case": "description", "industry": "industry or 'horizontal'", "product_category": "category", "workflow_demonstrated": "description", "job_to_be_done": "what the user is trying to accomplish", "keywords": ["relevant search terms for finding this demo"]}"""
    },
}


async def scan_layer(demo_entry: dict, layer_name: str, api_key: str,
                     tier_override: str = None, query_context: str = None) -> dict:
    """
    Run a targeted scan layer on a single demo.
    If query_context is provided, it's injected so the scan focuses on what
    the user specifically asked about (context-aware deep scan).
    Returns the layer result dict.
    """
    import anthropic

    layer = SCAN_LAYERS.get(layer_name)
    if not layer:
        return {"error": f"Unknown layer: {layer_name}"}

    steps_text = demo_entry.get("steps_text", [])
    if not steps_text:
        return {"error": "No step text available -- demo needs base scan first"}

    tier = tier_override or layer["tier"]
    model = get_model(tier, api_key)
    use_screenshots = layer["needs_screenshots"]

    # Build context-aware preamble if this scan was triggered by a query
    query_preamble = ""
    if query_context:
        query_preamble = f"""
CONTEXT: This scan was triggered by the user's query: "{query_context}"
Pay special attention to elements relevant to this query. In your response,
include an extra field "query_insight" with a 1-sentence finding about how
this demo relates to the query.

"""

    # Build content
    content = []

    # Intro
    demo_name = demo_entry.get("name", "Unknown")
    content.append({"type": "text", "text": f"""You are analyzing a product demo from "{demo_name}" ({len(steps_text)} steps).
{query_preamble}
{layer['prompt']}

## DEMO CONTENT
"""})

    if use_screenshots:
        # Load screenshots if available
        demo_dir = SCREENSHOTS_DIR / _safe_filename(demo_name)
        for i, step in enumerate(steps_text):
            step_text = f"Step {step.get('step', i+1)}/{step.get('total', len(steps_text))}: {step.get('text', '')}"
            content.append({"type": "text", "text": step_text})

            # Try to find screenshot
            screenshot_path = demo_dir / f"step_{i+1:03d}.png"
            if screenshot_path.exists():
                img_data = base64.standard_b64encode(screenshot_path.read_bytes()).decode("utf-8")
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img_data}
                })

        # If no screenshots found, fall back to text-only
        has_any_screenshots = any(
            (demo_dir / f"step_{i+1:03d}.png").exists()
            for i in range(len(steps_text))
        ) if demo_dir.exists() else False

        if not has_any_screenshots:
            # Text-only fallback
            content = [{"type": "text", "text": f"""You are analyzing a product demo from "{demo_name}" ({len(steps_text)} steps).
Note: Screenshots are not available. Analyze based on text content only.

{layer['prompt']}

## DEMO CONTENT
"""}]
            for step in steps_text:
                content.append({"type": "text", "text":
                    f"Step {step.get('step', '?')}/{step.get('total', '?')}: {step.get('text', '')}"
                })
    else:
        for step in steps_text:
            content.append({"type": "text", "text":
                f"Step {step.get('step', '?')}/{step.get('total', '?')}: {step.get('text', '')}"
            })

    content.append({"type": "text", "text": "\nRespond with ONLY a JSON object. No other text."})

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = call_with_fallback(
            client, model,
            max_tokens=1000,
            messages=[{"role": "user", "content": content}],
        )

        response_text = response.content[0].text.strip()
        result = _extract_json(response_text)

        if result is None:
            return {"error": "Failed to parse JSON", "raw": response_text[:300]}

        result["_layer"] = layer_name
        result["_model"] = "Haiku" if "haiku" in model else "Sonnet"
        result["_scanned_at"] = datetime.now().isoformat()
        result["_had_screenshots"] = use_screenshots and has_any_screenshots if use_screenshots else False
        return result

    except Exception as e:
        return {"error": str(e)[:200], "_layer": layer_name}


def get_demo_layers(demo_entry: dict) -> dict:
    """Get all scanned layers for a demo."""
    return demo_entry.get("layers", {})


def has_layer(demo_entry: dict, layer_name: str) -> bool:
    """Check if a demo has been scanned for a specific layer."""
    layers = demo_entry.get("layers", {})
    return layer_name in layers and "error" not in layers[layer_name]


def estimate_layer_cost(demos: list, layer_name: str) -> dict:
    """Estimate the cost of scanning a layer across a list of demos."""
    layer = SCAN_LAYERS.get(layer_name, {})
    needs_screenshots = layer.get("needs_screenshots", False)
    tier = layer.get("tier", "haiku")

    # Rough cost per demo
    if needs_screenshots:
        cost_per_demo = 0.02 if tier == "haiku" else 0.15
    else:
        cost_per_demo = 0.001 if tier == "haiku" else 0.01

    already_scanned = sum(1 for d in demos if has_layer(d, layer_name))
    to_scan = len(demos) - already_scanned

    return {
        "layer": layer_name,
        "total_demos": len(demos),
        "already_scanned": already_scanned,
        "to_scan": to_scan,
        "needs_screenshots": needs_screenshots,
        "tier": tier,
        "estimated_cost": round(to_scan * cost_per_demo, 3),
        "cost_per_demo": cost_per_demo,
    }


# ---------------------------------------------------------------------------
# Query Knowledge: learning loop — queries make the index smarter
# ---------------------------------------------------------------------------

def load_query_knowledge() -> dict:
    """Load the query knowledge log. Thin file — themes, findings, matched keys."""
    if QUERY_KNOWLEDGE_FILE.exists():
        try:
            return json.loads(QUERY_KNOWLEDGE_FILE.read_text())
        except Exception:
            pass
    return {"queries": [], "theme_index": {}}


def save_query_knowledge(knowledge: dict):
    """Persist query knowledge to disk."""
    QUERY_KNOWLEDGE_FILE.write_text(json.dumps(knowledge, indent=2, default=str))


def extract_query_themes(query: str, api_key: str) -> list[str]:
    """
    Extract 1-3 reusable theme tags from a query using Haiku.
    e.g. "best demo for social proof" → ["social-proof"]
    e.g. "which demos have strong onboarding flows?" → ["onboarding", "user-flow"]
    Themes are kebab-case, short, reusable across queries.
    """
    import anthropic

    prompt = f"""Extract 1-3 reusable theme tags from this demo library query. Tags should be:
- kebab-case (e.g. "social-proof", "narrative-quality", "onboarding-flow")
- Short (1-3 words)
- Reusable across queries (not query-specific like "best-5")
- About the demo ATTRIBUTE being searched for

Query: "{query}"

Return ONLY a JSON array of strings, nothing else. Example: ["social-proof", "credibility"]"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        model = get_model("haiku", api_key)
        response = call_with_fallback(client, model, max_tokens=100,
                                      messages=[{"role": "user", "content": prompt}])
        text = response.content[0].text.strip()
        # Parse JSON array from response
        if "[" in text:
            text = text[text.index("["):text.rindex("]")+1]
        themes = json.loads(text)
        return [t.lower().strip() for t in themes if isinstance(t, str)][:3]
    except Exception:
        # Fallback: simple keyword extraction
        words = re.findall(r"[a-z]{4,}", query.lower())
        stopwords = {"demo", "demos", "which", "what", "best", "good", "show", "have",
                     "with", "that", "from", "this", "does", "most", "like", "find"}
        return [w for w in words if w not in stopwords][:2]


def record_query_learning(query: str, themes: list[str], matched_keys: list[str],
                          finding_summary: str):
    """
    Record a query result into the knowledge log. Deduplicates themes —
    if a very similar query was asked before, it updates rather than appends.
    """
    knowledge = load_query_knowledge()

    # Check for duplicate theme combo
    theme_set = set(themes)
    existing_idx = None
    for i, entry in enumerate(knowledge["queries"]):
        if set(entry.get("themes", [])) == theme_set:
            existing_idx = i
            break

    record = {
        "q": query,
        "ts": datetime.now().isoformat(),
        "themes": themes,
        "matched_keys": matched_keys[:10],  # Cap to keep it thin
        "finding": finding_summary[:300],   # Compact summary
    }

    if existing_idx is not None:
        # Update existing — newer query replaces, but merge matched keys
        old = knowledge["queries"][existing_idx]
        merged_keys = list(dict.fromkeys(old.get("matched_keys", []) + matched_keys[:10]))[:15]
        record["matched_keys"] = merged_keys
        knowledge["queries"][existing_idx] = record
    else:
        knowledge["queries"].append(record)

    # Rebuild theme_index: theme → list of demo keys known to match
    theme_index = {}
    for entry in knowledge["queries"]:
        for theme in entry.get("themes", []):
            if theme not in theme_index:
                theme_index[theme] = []
            for k in entry.get("matched_keys", []):
                if k not in theme_index[theme]:
                    theme_index[theme].append(k)
    knowledge["theme_index"] = theme_index

    save_query_knowledge(knowledge)
    return knowledge


def tag_demos_from_query(index: dict, themes: list[str], matched_keys: list[str]):
    """
    Add theme-derived tags to matched demos. Tags are prefixed with 'q:'
    to distinguish query-learned tags from scan-derived tags.
    """
    for key in matched_keys:
        demo = index["demos"].get(key)
        if not demo:
            continue
        existing_tags = set(demo.get("tags", []))
        for theme in themes:
            tag = f"q:{theme}"
            existing_tags.add(tag)
        demo["tags"] = sorted(existing_tags)


def add_demo_insight(demo: dict, theme: str, insight_text: str):
    """
    Add a compact insight to a demo's insights dict. If the theme already
    exists, only update if the new insight is longer (deeper scan result).
    """
    if "insights" not in demo:
        demo["insights"] = {}
    existing = demo["insights"].get(theme, "")
    if len(insight_text) > len(existing):
        demo["insights"][theme] = insight_text[:200]  # Cap at 200 chars


def get_prior_knowledge_for_query(query: str, themes: list[str]) -> str:
    """
    Check if we have prior query knowledge relevant to this query.
    Returns a context string for the AI, or empty string.
    """
    knowledge = load_query_knowledge()
    if not knowledge["queries"]:
        return ""

    relevant = []
    theme_set = set(themes)
    query_words = set(re.findall(r"[a-z]{4,}", query.lower()))

    for entry in knowledge["queries"]:
        entry_themes = set(entry.get("themes", []))
        # Score by theme overlap + word overlap
        theme_overlap = len(theme_set & entry_themes)
        word_overlap = len(query_words & set(re.findall(r"[a-z]{4,}", entry["q"].lower())))
        if theme_overlap > 0 or word_overlap >= 2:
            relevant.append((theme_overlap * 3 + word_overlap, entry))

    if not relevant:
        return ""

    relevant.sort(key=lambda x: -x[0])
    lines = []
    for _, entry in relevant[:3]:
        lines.append(f"- Prior query \"{entry['q']}\": {entry['finding']}")

    return "\nPrior knowledge from earlier queries:\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Query Engine: parse intent → check index → shortlist → scan → return
# ---------------------------------------------------------------------------

def parse_query_intent(query: str, api_key: str) -> dict:
    """
    Use Haiku to understand what the user is looking for and which layers are needed.
    Returns: required layers, search keywords, query type.
    """
    import anthropic

    layer_descriptions = "\n".join([
        f"- {name}: {layer['label']} -- {layer['description']}"
        for name, layer in SCAN_LAYERS.items()
    ])

    prompt = f"""You are a query parser for a demo library. The user wants to find specific demos from a collection of product demos.

## AVAILABLE SCAN LAYERS
{layer_descriptions}

## USER QUERY
"{query}"

Analyze this query and return a JSON object:
{{
  "intent": "search|analyze|compare|recommend",
  "required_layers": ["list of layer names needed to answer this query"],
  "keywords": ["search terms to find candidate demos in the existing index"],
  "filters": {{"min_score": null, "type": null}},
  "needs_deep_scan": true/false,
  "explanation": "one sentence explaining what you'll look for"
}}

Rules:
- If the query asks about storytelling/narrative → include "narrative_quality"
- If about social proof/credibility/logos → include "social_proof"
- If about personalization/roles/personas → include "persona_targeting"
- If about customization/branding → include "customization"
- If about visual quality/design → include "visual_design"
- If about use cases/industry/what a demo shows → include "use_case"
- "needs_deep_scan" should be true if existing base classification likely can't answer this
- If the query is just looking for "good demos" or high scores, layers may not be needed
- keywords should be terms likely found in demo step text or summaries

Respond with ONLY the JSON object."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        model = get_model("haiku", api_key)
        response = call_with_fallback(
            client, model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        result = _extract_json(response.content[0].text.strip())
        if result:
            return result
    except Exception as e:
        print(f"   Intent parsing failed: {e}")

    # Fallback: basic keyword extraction
    return {
        "intent": "search",
        "required_layers": [],
        "keywords": query.lower().split(),
        "filters": {},
        "needs_deep_scan": False,
        "explanation": f"Searching for: {query}",
    }


def shortlist_candidates(index: dict, intent: dict) -> list:
    """
    Use existing index data to find demos that might match the query.
    No API calls -- pure text/metadata search on cached data.
    """
    keywords = intent.get("keywords", [])
    required_layers = intent.get("required_layers", [])
    filters = intent.get("filters", {})

    all_demos = list(index["demos"].values())

    # Apply hard filters first
    if filters.get("min_score"):
        all_demos = [d for d in all_demos
                    if (d.get("classification", {}).get("overall_score", 0) or 0) >= filters["min_score"]]
    if filters.get("type"):
        all_demos = [d for d in all_demos
                    if filters["type"].lower() in (d.get("classification", {}).get("type", "")).lower()]

    # Score each demo by keyword relevance + text signal matching
    scored = []
    for demo in all_demos:
        score = 0

        # Build searchable text from all available data
        cls = demo.get("classification", {})
        def _to_str(val):
            if isinstance(val, str):
                return val
            if isinstance(val, list):
                return " ".join(str(x) for x in val)
            return str(val) if val else ""

        parts = [
            demo.get("name", ""),
            demo.get("category", ""),
            _to_str(cls.get("type", "")),
            _to_str(cls.get("summary", "")),
            _to_str(cls.get("strengths", [])),
            _to_str(cls.get("weaknesses", [])),
            _to_str(cls.get("recommendation", "")),
            _to_str(cls.get("narrative_arc", "")),
            _to_str(cls.get("persona_targeting", "")),
            _to_str(cls.get("proof_elements", "")),
            " ".join(demo.get("tags", [])),
        ]
        # Add step text
        for step in demo.get("steps_text", []):
            parts.append(step.get("text", ""))

        # Add existing layer results
        for layer_name, layer_data in demo.get("layers", {}).items():
            if isinstance(layer_data, dict):
                for v in layer_data.values():
                    if isinstance(v, str):
                        parts.append(v)
                    elif isinstance(v, list):
                        parts.extend(str(item) for item in v)

        # Add insights from prior queries
        if demo.get("insights"):
            for insight_text in demo["insights"].values():
                parts.append(insight_text)

        searchable = " ".join(parts).lower()

        # Keyword matching
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in searchable:
                score += 10
                # Bonus for exact word match
                if f" {kw_lower} " in f" {searchable} ":
                    score += 5

        # Bonus for demos known to match related themes from prior queries
        knowledge = load_query_knowledge()
        theme_index = knowledge.get("theme_index", {})
        demo_key = demo.get("key", "")
        for kw in keywords:
            kw_lower = kw.lower()
            for theme, theme_keys in theme_index.items():
                if kw_lower in theme and demo_key in theme_keys:
                    score += 15  # Strong signal — prior query confirmed relevance

        # Text signal matching for required layers
        for layer_name in required_layers:
            layer = SCAN_LAYERS.get(layer_name, {})
            for signal in layer.get("text_signals", []):
                if signal.lower() in searchable:
                    score += 3

        # Bonus for already having required layers scanned
        for layer_name in required_layers:
            if has_layer(demo, layer_name):
                score += 20  # Big bonus -- we already have data

        # Bonus for having base classification
        if cls.get("summary"):
            score += 2

        # Must have steps to be scannable
        if not demo.get("steps_text") and not demo.get("last_scanned_at"):
            score -= 50  # Penalty -- can't scan without base data

        if score > 0:
            scored.append((score, demo))

    scored.sort(key=lambda x: -x[0])
    return [s[1] for s in scored]


def build_query_plan(index: dict, intent: dict, candidates: list) -> dict:
    """
    Build a plan for answering the query: what's already known, what needs scanning.
    """
    required_layers = intent.get("required_layers", [])

    # Separate into already-answered and needs-scan
    already_have = []
    need_scan = []
    need_base_scan = []

    for demo in candidates:
        has_all_layers = all(has_layer(demo, l) for l in required_layers) if required_layers else True
        has_base = bool(demo.get("steps_text"))

        if has_all_layers and has_base:
            already_have.append(demo)
        elif has_base:
            need_scan.append(demo)
        else:
            need_base_scan.append(demo)

    # Cost estimation
    layer_costs = {}
    total_cost = 0.0
    for layer_name in required_layers:
        est = estimate_layer_cost(need_scan, layer_name)
        layer_costs[layer_name] = est
        total_cost += est["estimated_cost"]

    # Base scan cost for demos without steps
    base_scan_cost = len(need_base_scan) * 0.002  # Haiku text classification

    return {
        "intent": intent,
        "total_candidates": len(candidates),
        "already_have_data": len(already_have),
        "need_layer_scan": len(need_scan),
        "need_base_scan": len(need_base_scan),
        "layer_costs": layer_costs,
        "total_estimated_cost": round(total_cost + base_scan_cost, 3),
        "candidates_with_data": already_have,
        "candidates_need_scan": need_scan,
        "candidates_need_base": need_base_scan,
    }


async def answer_from_existing(query: str, candidates: list, intent: dict, api_key: str) -> str:
    """
    Use Claude to answer the query using only existing indexed data.
    Returns a formatted answer string.
    """
    import anthropic

    # Build compact demo summaries
    demo_summaries = []
    for i, d in enumerate(candidates[:30]):  # Cap at 30 for context window
        cls = d.get("classification", {})
        name = d.get('display_name') or build_display_name(d)
        url = d.get('demo_url', '') or d.get('showcase_url', '') or d.get('live_preview_url', '')
        parts = [f"#{i+1} {name}"]
        if url:
            parts.append(f"url={url}")
        if cls.get("type"):
            parts.append(f"type={cls['type']}")
        if cls.get("overall_score"):
            parts.append(f"score={cls['overall_score']}/10")
        if cls.get("summary"):
            parts.append(f"summary: {cls['summary'][:200]}")

        # Include relevant layer data
        for layer_name in intent.get("required_layers", []):
            layer_data = d.get("layers", {}).get(layer_name, {})
            if layer_data and "error" not in layer_data:
                # Flatten layer to key info
                layer_parts = []
                for k, v in layer_data.items():
                    if k.startswith("_"):
                        continue
                    if isinstance(v, (str, int, float, bool)):
                        layer_parts.append(f"{k}={v}")
                    elif isinstance(v, list) and v:
                        layer_parts.append(f"{k}={', '.join(str(x) for x in v[:5])}")
                if layer_parts:
                    parts.append(f"[{layer_name}]: {'; '.join(layer_parts)}")

        if cls.get("strengths"):
            strengths = cls["strengths"] if isinstance(cls["strengths"], list) else [cls["strengths"]]
            parts.append(f"strengths: {'; '.join(strengths[:3])}")
        if d.get("tags"):
            parts.append(f"tags: {', '.join(d['tags'])}")
        # Include per-demo insights from prior queries/scans
        if d.get("insights"):
            insight_parts = [f"{k}: {v}" for k, v in d["insights"].items()]
            parts.append(f"insights: {'; '.join(insight_parts[:5])}")

        demo_summaries.append(" | ".join(parts))

    # Include prior knowledge from earlier queries
    themes = extract_query_themes(query, api_key) if api_key else []
    prior_knowledge = get_prior_knowledge_for_query(query, themes) if themes else ""

    # Include relevant tips from knowledge base
    kb = load_knowledge_base()
    tips_section = ""
    if kb.get("tips"):
        query_lower = query.lower()
        relevant_tips = []
        for tip in kb["tips"]:
            tip_text = tip["tip"].lower()
            tip_cat = tip.get("category", "").lower()
            tip_reasoning = tip.get("reasoning", "").lower()
            # Score relevance to query
            score = sum(1 for word in query_lower.split() if len(word) > 3 and
                       (word in tip_text or word in tip_cat or word in tip_reasoning))
            if score > 0:
                relevant_tips.append((score, tip))
        relevant_tips.sort(key=lambda x: -x[0])
        if relevant_tips:
            tip_lines = []
            for _, tip in relevant_tips[:8]:
                example = f" (example: {tip['company']})" if tip.get("company") else ""
                tip_lines.append(f"- [{tip.get('category', '')}] {tip['tip']}{example}")
            tips_section = f"\n\nRelevant best practices from our knowledge base:\n" + "\n".join(tip_lines)

    # Include auto-suggestions for top candidates
    suggestions_section = ""
    demo_suggestions = []
    for d in candidates[:10]:
        sug = d.get("auto_suggestions", [])
        if sug:
            demo_suggestions.append(f"- {d.get('name', '?')}: {'; '.join(s['suggestion'][:80] for s in sug[:3])}")
    if demo_suggestions:
        suggestions_section = f"\n\nAutomated improvement suggestions for these demos:\n" + "\n".join(demo_suggestions)

    prompt = f"""You are a demo library assistant with expertise in interactive demo best practices. The user asked:

"{query}"
{prior_knowledge}
Here are the relevant demos from the library:

{chr(10).join(demo_summaries)}{tips_section}{suggestions_section}

Answer the query directly. Be specific -- reference demos by name AND include their URL as a markdown link like [Demo Name](url) so the user can open the demo immediately. Explain why each match is relevant. When relevant, include best practice tips and improvement suggestions. If data is limited, say what additional scanning would help. Keep it concise and actionable.

IMPORTANT: After your answer, add a line "---THEMES:" followed by 1-3 word descriptors for each matched demo explaining WHY it matched this query. Format: demo_index:one-line-reason. Example:
---THEMES:
1:Strong customer logo wall in opening screen
3:ROI metrics woven through narrative

Format your response as:
1. Direct answer to the query
2. Top recommendations with brief reasons — each demo name MUST be a clickable markdown link [Name](url)
3. Any gaps where deeper scanning would help
4. ---THEMES: section (for system use, will be hidden from display)"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        model = get_model("haiku", api_key)
        response = call_with_fallback(
            client, model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_answer = response.content[0].text.strip()

        # --- Learning loop: parse THEMES, tag demos, record knowledge ---
        display_answer = raw_answer
        matched_keys = []
        if "---THEMES:" in raw_answer:
            parts = raw_answer.split("---THEMES:", 1)
            display_answer = parts[0].strip()
            themes_block = parts[1].strip()

            # Parse per-demo insights from the THEMES block
            live_index = load_index()
            for line in themes_block.splitlines():
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"(\d+)\s*[:\.]\s*(.+)", line)
                if m:
                    demo_idx = int(m.group(1)) - 1  # 1-indexed in prompt
                    insight = m.group(2).strip()
                    if 0 <= demo_idx < len(candidates):
                        cand = candidates[demo_idx]
                        demo_key = cand.get("key", "")
                        if demo_key:
                            matched_keys.append(demo_key)
                            # Write insight to the demo in the index
                            idx_demo = live_index["demos"].get(demo_key)
                            if idx_demo:
                                for theme in themes:
                                    add_demo_insight(idx_demo, theme, insight)

            # Tag matched demos and save
            if themes and matched_keys:
                tag_demos_from_query(live_index, themes, matched_keys)
                save_index(live_index)

                # Build a compact finding summary for the knowledge log
                finding = display_answer[:200]
                if len(display_answer) > 200:
                    # Take first sentence or two
                    sentences = re.split(r'[.!?]\s', display_answer[:400])
                    finding = '. '.join(sentences[:2]) + '.'
                record_query_learning(query, themes, matched_keys, finding)

        return display_answer
    except Exception as e:
        return f"Error generating answer: {e}"


async def execute_layer_scans(index: dict, demos: list, layer_names: list,
                              api_key: str, query_context: str = None) -> int:
    """
    Run layer scans on a list of demos. Saves results to index after each demo.
    If query_context is provided, each layer scan gets the query injected so
    the AI focuses on what matters for the user's question.
    Returns number of demos scanned.
    """
    scanned = 0
    total = len(demos) * len(layer_names)
    done = 0

    # If triggered from a query, extract themes for insight storage
    themes = []
    if query_context and api_key:
        try:
            themes = extract_query_themes(query_context, api_key)
        except Exception:
            pass

    for demo in demos:
        key = None
        for k, d in index["demos"].items():
            if d is demo:
                key = k
                break
        if not key:
            continue

        for layer_name in layer_names:
            done += 1
            if has_layer(demo, layer_name):
                continue

            print(f"   [{done}/{total}] {demo.get('name', '?')} -- {layer_name}...", end=" ")
            result = await scan_layer(demo, layer_name, api_key,
                                      query_context=query_context)

            if "layers" not in demo:
                demo["layers"] = {}
            demo["layers"][layer_name] = result

            if "error" in result:
                print(f"Error: {result['error'][:50]}")
            else:
                # Extract a score if present
                score_key = [k for k in result.keys() if k.endswith("_score") and not k.startswith("_")]
                if score_key:
                    print(f"OK (score: {result[score_key[0]]}/10)")
                else:
                    print("OK")

                # Save query_insight from context-aware scan back to demo insights
                if query_context and themes and result.get("query_insight"):
                    for theme in themes:
                        add_demo_insight(demo, theme, result["query_insight"])

            index["demos"][key] = demo
            save_index(index)
            scanned += 1

    return scanned


async def run_query(query: str, api_key: str, auto_scan: bool = False):
    """
    Full query pipeline: parse → shortlist → plan → answer (→ scan if approved).
    """
    index = load_index()

    if not index["demos"]:
        print("   Index is empty. Run a sync first.")
        return

    print(f"\n{'='*60}")
    print(f"QUERY: {query}")
    print(f"{'='*60}")

    # Step 1: Parse intent
    print("\n   [1/4] Understanding query...")
    intent = parse_query_intent(query, api_key)
    print(f"   Intent: {intent.get('explanation', '?')}")
    if intent.get("required_layers"):
        print(f"   Layers needed: {', '.join(intent['required_layers'])}")

    # Step 2: Shortlist candidates
    print("\n   [2/4] Searching index...")
    candidates = shortlist_candidates(index, intent)
    print(f"   Found {len(candidates)} candidate demos")

    if not candidates:
        print("   No demos match this query. Try different terms or import more demos.")
        return

    # Step 3: Build plan
    print("\n   [3/4] Building plan...")
    plan = build_query_plan(index, intent, candidates)
    print(f"   Already have data: {plan['already_have_data']} demos")
    print(f"   Need layer scan:  {plan['need_layer_scan']} demos")
    print(f"   Need base scan:   {plan['need_base_scan']} demos")

    if plan["total_estimated_cost"] > 0:
        print(f"   Estimated scan cost: ~${plan['total_estimated_cost']:.3f}")
        for layer_name, cost in plan["layer_costs"].items():
            layer_label = SCAN_LAYERS[layer_name]["label"]
            print(f"      {layer_label}: {cost['to_scan']} demos @ ~${cost['cost_per_demo']}/demo = ~${cost['estimated_cost']:.3f}")

    # Step 4: Answer with what we have
    print("\n   [4/4] Generating answer from existing data...")
    top_candidates = candidates[:20]
    answer = await answer_from_existing(query, top_candidates, intent, api_key)

    print(f"\n{'─'*60}")
    print(answer)
    print(f"{'─'*60}")

    # Offer to scan for more data
    if plan["need_layer_scan"] > 0 and intent.get("required_layers"):
        scan_count = min(plan["need_layer_scan"], 20)  # Cap at 20
        layers = intent["required_layers"]
        layer_labels = [SCAN_LAYERS[l]["label"] for l in layers if l in SCAN_LAYERS]
        cost = plan["total_estimated_cost"]

        print(f"\n   SCAN SUGGESTION: {scan_count} demos could be scanned for: {', '.join(layer_labels)}")
        print(f"   Estimated cost: ~${cost:.3f}")
        print(f"   Run: python3 run.py --scan-layer {','.join(layers)} --limit {scan_count}")

        if auto_scan:
            print(f"\n   Auto-scanning {scan_count} demos...")
            demos_to_scan = plan["candidates_need_scan"][:scan_count]
            scanned = await execute_layer_scans(index, demos_to_scan, layers, api_key)
            print(f"   Scanned {scanned} demos. Re-running query...")
            # Re-answer with new data
            index = load_index()
            candidates = shortlist_candidates(index, intent)
            answer = await answer_from_existing(query, candidates[:20], intent, api_key)
            print(f"\n{'─'*60}")
            print(answer)
            print(f"{'─'*60}")


async def run_layer_scan(layer_names: list, api_key: str, limit: int = 0,
                         target_query: str = None):
    """
    Batch scan one or more layers across demos in the index.
    If target_query is provided, only scan relevant demos.
    """
    index = load_index()

    # Validate layers
    valid_layers = []
    for name in layer_names:
        if name in SCAN_LAYERS:
            valid_layers.append(name)
        else:
            print(f"   Unknown layer: {name}")
            print(f"   Available: {', '.join(SCAN_LAYERS.keys())}")
            return

    # Select demos
    if target_query:
        intent = parse_query_intent(target_query, api_key)
        demos = shortlist_candidates(index, intent)
    else:
        # All demos with base data
        demos = [d for d in index["demos"].values() if d.get("steps_text")]

    # Filter to demos that need scanning
    demos_to_scan = [d for d in demos if not all(has_layer(d, l) for l in valid_layers)]

    if limit > 0:
        demos_to_scan = demos_to_scan[:limit]

    if not demos_to_scan:
        print("   All candidate demos already have these layers scanned.")
        return

    # Cost estimate
    total_cost = 0
    for layer_name in valid_layers:
        est = estimate_layer_cost(demos_to_scan, layer_name)
        total_cost += est["estimated_cost"]
        layer_info = SCAN_LAYERS[layer_name]
        print(f"   Layer: {layer_info['label']}")
        print(f"      Demos to scan: {est['to_scan']}")
        print(f"      Screenshots: {'Yes' if est['needs_screenshots'] else 'No (text only)'}")
        print(f"      Model: {est['tier'].title()}")
        print(f"      Est. cost: ~${est['estimated_cost']:.3f}")

    print(f"\n   Total: {len(demos_to_scan)} demos x {len(valid_layers)} layer(s) = ~${total_cost:.3f}")
    print()

    # Scan
    scanned = await execute_layer_scans(index, demos_to_scan, valid_layers, api_key)
    generate_report(index)
    print(f"\n   Done. Scanned {scanned} demo-layers. Index updated.")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run_sync(args, api_key: str):
    """Main sync pipeline: scrape showcase, walk+classify new demos, update index."""
    index = load_index()
    existing_count = len(index["demos"])

    rescan_query = args.rescan if isinstance(args.rescan, str) and args.rescan != "True" else None
    force_rescan = (args.rescan is not None and args.rescan is not False) or bool(args.rescan_keys)

    mode_labels = {
        "fast": "Fast (text-only + Haiku)",
        "full": "Full (screenshots + Sonnet)",
        "smart": "Smart (Haiku first, Sonnet for top demos)",
    }

    print("=" * 70)
    print("STORYLANE DEMO CLASSIFIER")
    print(f"   Index: {existing_count} demos in database")
    if force_rescan:
        if rescan_query:
            print(f"   Mode: Rescan demos matching \"{rescan_query}\"")
        else:
            print(f"   Mode: Rescan ALL demos")
    else:
        print(f"   Mode: Sync (only process new demos)")
    print(f"   Classification: {mode_labels.get(args.mode, args.mode)}")
    print("=" * 70)

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(viewport=VIEWPORT)
        page = await context.new_page()

        # --- Handle single demo URL mode ---
        if args.demo_url:
            demo = DemoInfo(name="direct-demo", showcase_url="", demo_iframe_url=args.demo_url)
            print(f"\n   Processing single demo: {args.demo_url}")
            result = await walk_demo(page, demo, 0)

            if not args.no_classify and api_key:
                print(f"\n   Classifying demo...")
                result.classification = await classify_demo(result, mode=args.mode, criteria_file=args.criteria_file, api_key=api_key)

            entry = result.to_index_entry(source="direct")
            merge_demo_into_index(index, entry, force=True)
            save_index(index)
            generate_report(index)
            print_demo_table([index["demos"][_demo_key(args.demo_url)]], "Result")
            await browser.close()
            return

        # --- Scrape showcase ---
        demos = await scrape_showcase(page)

        # Register all discovered demos in the index (metadata only)
        new_count = 0
        for d in demos:
            was_new = merge_demo_into_index(index, {
                "name": d.name,
                "showcase_url": d.showcase_url,
                "category": d.category,
                "source": "showcase",
            })
            if was_new:
                new_count += 1

        print(f"\n   Index updated: {new_count} new demos discovered, {len(index['demos'])} total")

        if args.scrape_only:
            save_index(index)
            generate_report(index)
            print(f"\n   Index saved. Use --list to view all demos.")
            await browser.close()
            return

        # Add extra URLs if provided
        if args.extra_urls:
            extra_list = [u.strip() for u in args.extra_urls.split(",") if u.strip()]
            for url in extra_list:
                name = url.rstrip("/").split("/")[-1] or "custom-demo"
                name = name.replace("-", " ").replace("_", " ").title()
                entry = {"name": f"[Custom] {name}", "source": "import"}
                if "/demo/" in url:
                    entry["demo_url"] = url
                else:
                    entry["showcase_url"] = url
                merge_demo_into_index(index, entry)

        # Determine which demos to process
        if args.rescan_keys:
            # Rescan exact demos by key (used by UI per-demo and multi-select rescan)
            keys = [k.strip() for k in args.rescan_keys.split(",") if k.strip()]
            demos_to_process = [index["demos"][k] for k in keys if k in index["demos"]]
            print(f"\n   Rescan: {len(demos_to_process)} demos by key")
        elif force_rescan and rescan_query:
            # Rescan specific demos matching query
            matches = search_index(index, rescan_query)
            demos_to_process = [d for d in matches if d.get("demo_url") or d.get("showcase_url")]
            print(f"\n   Rescan: {len(demos_to_process)} demos match \"{rescan_query}\"")
        elif force_rescan:
            # Rescan everything
            demos_to_process = [d for d in index["demos"].values()
                               if (d.get("demo_url") or d.get("showcase_url"))
                               and d.get("is_accessible", True) and not d.get("is_gated")]
            print(f"\n   Rescan: {len(demos_to_process)} accessible demos")
        else:
            # Only process demos that haven't been scanned yet
            demos_to_process = [d for d in index["demos"].values()
                               if not d.get("last_scanned_at")
                               and d.get("is_accessible", True)]
            print(f"\n   New demos to process: {len(demos_to_process)}")

        if args.limit > 0:
            demos_to_process = demos_to_process[:args.limit]
            print(f"   Limited to first {args.limit}")

        if not demos_to_process:
            print("\n   Nothing new to process. Index is up to date.")
            save_index(index)
            generate_report(index)
            await browser.close()
            return

        # Step 2: Extract demo iframe URLs for demos that need it
        need_extraction = [d for d in demos_to_process if not d.get("demo_url") and d.get("showcase_url")]
        if need_extraction:
            print(f"\n--- STEP 2: Extracting demo URLs for {len(need_extraction)} demos ---")
            for i, demo_entry in enumerate(need_extraction):
                demo_info = DemoInfo(name=demo_entry["name"], showcase_url=demo_entry["showcase_url"])
                print(f"   [{i+1}/{len(need_extraction)}] {demo_entry['name']}...", end=" ")
                demo_info = await extract_demo_url(page, demo_info)
                # Update index with extracted URL
                merge_demo_into_index(index, {
                    "showcase_url": demo_entry["showcase_url"],
                    "demo_url": demo_info.demo_iframe_url,
                    "live_preview_url": demo_info.live_preview_url,
                    "is_gated": demo_info.is_gated,
                    "is_accessible": demo_info.is_accessible,
                    "error": demo_info.error,
                }, force=True)
                if demo_info.demo_iframe_url:
                    print(f"OK")
                    # Update the demo_to_process entry with the extracted URL
                    demo_entry["demo_url"] = demo_info.demo_iframe_url
                elif demo_info.is_gated:
                    print(f"Gated")
                else:
                    print(f"Failed: {demo_info.error[:50]}")

            save_index(index)

        # Filter to only accessible demos with URLs
        walkable = [d for d in demos_to_process
                    if d.get("demo_url") and d.get("is_accessible", True) and not d.get("is_gated")]

        print(f"\n--- STEP 3: Walking {len(walkable)} demos ---")

        do_classify = not args.no_classify and api_key
        mode = args.mode
        criteria_file = args.criteria_file

        for i, demo_entry in enumerate(walkable):
            name = demo_entry.get("name", "Unknown")
            demo_url = demo_entry["demo_url"]
            print(f"\n   [{i+1}/{len(walkable)}] {name}")

            demo_info = DemoInfo(
                name=name,
                showcase_url=demo_entry.get("showcase_url", ""),
                demo_iframe_url=demo_url,
                live_preview_url=demo_entry.get("live_preview_url", ""),
                category=demo_entry.get("category", ""),
            )

            result = await walk_demo(page, demo_info, i)

            if do_classify and result.steps:
                print(f"   Classifying...", end=" ")
                result.classification = await classify_demo(result, mode=mode, criteria_file=criteria_file, api_key=api_key)
                cls_type = result.classification.get("type", "unknown")
                score = result.classification.get("overall_score", result.classification.get("score", "?"))
                print(f"-> {cls_type} (score: {score}/10)")

            # Merge result into index
            entry = result.to_index_entry(source=demo_entry.get("source", "showcase"))
            entry["showcase_url"] = demo_entry.get("showcase_url", "")
            merge_demo_into_index(index, entry, force=True)

            # Save after each demo (partial progress)
            save_index(index)

        # Smart mode: re-classify top demos with Sonnet + screenshots
        if do_classify and mode == "smart":
            top_keys = [k for k, d in index["demos"].items()
                       if d.get("classification", {}).get("overall_score", 0) >= 6
                       and d.get("steps_text")]
            if top_keys:
                print(f"\n   Smart mode: Re-classifying {len(top_keys)} top demos with Sonnet + screenshots...")
                for i, key in enumerate(top_keys):
                    d = index["demos"][key]
                    # Reconstruct DemoResult for classification
                    demo_info = DemoInfo(
                        name=d["name"],
                        showcase_url=d.get("showcase_url", ""),
                        demo_iframe_url=d.get("demo_url", ""),
                    )
                    fake_result = DemoResult(info=demo_info)
                    for st in d.get("steps_text", []):
                        fake_result.steps.append(DemoStep(
                            step_number=st.get("step", 0),
                            total_steps=st.get("total", 0),
                            tooltip_text=st.get("text", ""),
                        ))
                    print(f"   [{i+1}/{len(top_keys)}] Re-classifying {d['name']}...", end=" ")
                    cls = await classify_demo(fake_result, mode="full", criteria_file=criteria_file, api_key=api_key)
                    d["classification"] = cls
                    d["last_scanned_at"] = datetime.now().isoformat()
                    score = cls.get("overall_score", cls.get("score", "?"))
                    print(f"-> {cls.get('type', '?')} (score: {score}/10)")
                save_index(index)

        # Final report
        generate_report(index)

        # Print summary
        stats = get_index_stats(index)
        print(f"\n{'=' * 70}")
        print(f"SYNC COMPLETE")
        print(f"   Total demos in index: {stats['total_demos']}")
        print(f"   Scanned: {stats['scanned']} | Unscanned: {stats['unscanned']}")
        print(f"   Avg score: {stats['avg_score']}/10")
        print(f"\n   Type breakdown:")
        for t, count in stats["type_breakdown"].items():
            print(f"      {t}: {count}")
        print(f"{'=' * 70}")

        await browser.close()

    print("\nDone!")


async def main():
    parser = argparse.ArgumentParser(description="Storylane Demo Classifier -- Persistent Demo Index")

    # Modes
    parser.add_argument("--sync", action="store_true", default=True,
                        help="Sync: scrape showcase, walk+classify NEW demos only (default)")
    parser.add_argument("--rescan", nargs="?", const=True, default=False,
                        help="Force re-scan demos. Optionally pass a search query to rescan specific demos.")
    parser.add_argument("--search", type=str, default=None,
                        help="Search the index by name/category/type/tags (keyword match)")
    parser.add_argument("--query", type=str, default=None,
                        help="Smart query: AI-powered search with intent parsing, layer suggestions, and cost estimation")
    parser.add_argument("--scan-layer", type=str, default=None, dest="scan_layer",
                        help="Run a specific scan layer (e.g. 'social_proof', 'persona_targeting'). Comma-separate for multiple.")
    parser.add_argument("--layers", action="store_true",
                        help="List available scan layers")
    parser.add_argument("--list", action="store_true",
                        help="List all demos in the index")
    parser.add_argument("--stats", action="store_true",
                        help="Show index statistics")
    parser.add_argument("--import-file", type=str, default=None, dest="import_file",
                        help="Bulk import demo URLs from a file (one per line)")
    parser.add_argument("--import-urls", type=str, default=None, dest="import_urls_str",
                        help="Import comma-separated demo URLs")

    # Pipeline options
    parser.add_argument("--scrape-only", action="store_true", help="Only scrape demo URLs, don't walk or classify")
    parser.add_argument("--no-classify", action="store_true", help="Walk demos but skip classification")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of demos to process")
    parser.add_argument("--demo-url", type=str, help="Process a single demo URL directly")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode (visible)")
    parser.add_argument("--mode", type=str, default="fast", choices=["fast", "full", "smart"],
                        help="Classification mode: fast (text+Haiku), full (screenshots+Sonnet), smart (both)")
    parser.add_argument("--criteria-file", type=str, default=None,
                        help="Path to a custom classification rubric file")
    parser.add_argument("--extra-urls", type=str, default=None,
                        help="Comma-separated list of additional demo URLs to process")
    parser.add_argument("--api-key", type=str, default=None,
                        help="Anthropic API key (alternative to ANTHROPIC_API_KEY env var)")
    parser.add_argument("--retrieve-screenshots", type=str, default=None, dest="retrieve_screenshots",
                        help="Re-walk specific demos to capture missing screenshots. Comma-separated demo keys.")
    parser.add_argument("--rescan-keys", type=str, default=None, dest="rescan_keys",
                        help="Re-scan specific demos by exact key. Comma-separated keys.")

    # Filter options (for --list)
    parser.add_argument("--type", type=str, default=None, dest="type_filter",
                        help="Filter by classification type (for --list)")
    parser.add_argument("--min-score", type=float, default=None,
                        help="Filter by minimum overall score (for --list)")

    args = parser.parse_args()

    api_key = args.api_key or ANTHROPIC_API_KEY

    global HEADLESS
    if args.headed:
        HEADLESS = False

    # --- Search mode (keyword) ---
    if args.search:
        index = load_index()
        results = search_index(index, args.search)
        print_demo_table(results, f"Search: \"{args.search}\"")
        if results:
            # Show detailed info for top 3
            for d in results[:3]:
                cls = d.get("classification", {})
                if cls.get("summary"):
                    print(f"\n   >> {d['name']}")
                    print(f"      {cls['summary']}")
                    if cls.get("strengths"):
                        print(f"      Strengths: {'; '.join(cls['strengths'][:3])}")
        return

    # --- Smart query mode (full engine) ---
    if args.query:
        if not api_key:
            print("   --query requires an API key (--api-key or ANTHROPIC_API_KEY env var)")
            return
        await run_query(args.query, api_key)
        return

    # --- Scan layer mode ---
    if args.scan_layer:
        if not api_key:
            print("   --scan-layer requires an API key")
            return
        layer_names = [l.strip() for l in args.scan_layer.split(",")]
        await run_layer_scan(layer_names, api_key, limit=args.limit)
        return

    # --- Retrieve missing screenshots ---
    if args.retrieve_screenshots:
        target_keys = set(k.strip() for k in args.retrieve_screenshots.split(",") if k.strip())
        index = load_index()
        demos_to_walk = [d for k, d in index["demos"].items()
                         if k in target_keys and d.get("demo_url") and d.get("is_accessible", True)]
        if not demos_to_walk:
            print("   No matching walkable demos found for the provided keys.")
            return
        print(f"   Retrieving screenshots for {len(demos_to_walk)} demo(s)...")
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            context = await browser.new_context(viewport=VIEWPORT)
            page = await context.new_page()
            for i, demo_entry in enumerate(demos_to_walk):
                name = demo_entry.get("name", "Unknown")
                print(f"\n   [{i+1}/{len(demos_to_walk)}] Walking {name}...")
                demo_info = DemoInfo(
                    name=name,
                    showcase_url=demo_entry.get("showcase_url", ""),
                    demo_iframe_url=demo_entry["demo_url"],
                )
                result = await walk_demo(page, demo_info, i)
                if result.steps_captured > 0:
                    entry = result.to_index_entry(source=demo_entry.get("source", "showcase"))
                    entry["showcase_url"] = demo_entry.get("showcase_url", "")
                    merge_demo_into_index(index, entry, force=True)
                    save_index(index)
                    print(f"      Captured {result.steps_captured} screenshots.")
                else:
                    print(f"      No screenshots captured (demo may have changed).")
            await browser.close()
        print(f"\n   Done. Screenshots updated.")
        return

    # --- List layers ---
    if args.layers:
        print(f"\n   AVAILABLE SCAN LAYERS")
        print(f"   {'='*60}")
        for name, layer in SCAN_LAYERS.items():
            screenshots = "screenshots" if layer["needs_screenshots"] else "text only"
            tier = layer["tier"].title()
            print(f"\n   {name}")
            print(f"      {layer['label']}")
            print(f"      {layer['description']}")
            print(f"      Mode: {screenshots} | Model: {tier}")
            if layer.get("upgrade_to"):
                print(f"      Upgrade: Can re-scan with {layer['upgrade_to'].title()} for higher accuracy")
        print(f"\n   Usage: python3 run.py --scan-layer social_proof --limit 10")
        print(f"          python3 run.py --scan-layer social_proof,persona_targeting")
        return

    # --- List mode ---
    if args.list:
        index = load_index()
        results = filter_index(index, type_filter=args.type_filter, min_score=args.min_score)
        title = "All Demos"
        if args.type_filter:
            title += f" (type: {args.type_filter})"
        if args.min_score:
            title += f" (score >= {args.min_score})"
        print_demo_table(results, title)
        return

    # --- Stats mode ---
    if args.stats:
        index = load_index()
        stats = get_index_stats(index)
        print(f"\n{'=' * 50}")
        print(f"DEMO INDEX STATS")
        print(f"{'=' * 50}")
        print(f"   Total demos:    {stats['total_demos']}")
        print(f"   Scanned:        {stats['scanned']}")
        print(f"   Unscanned:      {stats['unscanned']}")
        print(f"   Accessible:     {stats['accessible']}")
        print(f"   Gated:          {stats['gated']}")
        print(f"   Avg score:      {stats['avg_score']}/10")
        print(f"   Highest score:  {stats['highest_score']}/10")
        print(f"   Lowest score:   {stats['lowest_score']}/10")
        print(f"   Last sync:      {stats['last_sync'] or 'never'}")
        print(f"\n   Type breakdown:")
        for t, count in stats["type_breakdown"].items():
            print(f"      {t}: {count}")
        print(f"\n   Source breakdown:")
        for s, count in stats["source_breakdown"].items():
            print(f"      {s}: {count}")
        print(f"{'=' * 50}")
        return

    # --- Import mode ---
    if args.import_file or args.import_urls_str:
        index = load_index()
        urls = []
        if args.import_file:
            urls = import_urls_from_file(args.import_file)
            print(f"\n   Importing from file: {args.import_file}")
        if args.import_urls_str:
            urls.extend(import_urls_from_string(args.import_urls_str))

        if not urls:
            print("   No valid URLs found to import.")
            return

        print(f"   Found {len(urls)} URLs to import")
        added = 0
        for url in urls:
            name = url.rstrip("/").split("/")[-1] or "imported-demo"
            name = name.replace("-", " ").replace("_", " ").title()
            entry = {"name": name, "source": "import"}
            if "/demo/" in url:
                entry["demo_url"] = url
            else:
                entry["showcase_url"] = url
            was_new = merge_demo_into_index(index, entry)
            if was_new:
                added += 1
                print(f"      + {name}")
            else:
                print(f"      = {name} (already in index)")

        save_index(index)
        print(f"\n   Imported: {added} new, {len(urls) - added} already existed")
        print(f"   Total demos in index: {len(index['demos'])}")
        print(f"   Run without flags to sync and classify new demos.")
        return

    # --- Default: Sync pipeline ---
    await run_sync(args, api_key)


if __name__ == "__main__":
    asyncio.run(main())
