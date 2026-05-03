import re
import json
import base64
import time
from pathlib import Path
import config
from modules.model_manager import create_message

# ── Format detection keywords ────────────────────────────────────────────────
_FORMAT_KEYWORDS = {
    "comparison": ["vs", "versus", "compare", "comparison", "difference", "alternative"],
    "process": ["step", "process", "how to", "workflow", "stages", "phases", "flow"],
    "data_bar": ["bar chart", "ranking", "adoption", "survey", "percentage", "breakdown"],
    "data_pie": ["pie", "donut", "distribution", "share", "proportion", "split"],
    "data_curve": ["growth", "trend", "over time", "curve", "line chart", "increase"],
    "data_scatter": ["scatter", "correlation", "cluster", "relationship"],
    "carousel": ["carousel", "slides", "multi-slide", "series"],
    "resource_guide": ["tools", "resources", "list of", "guide", "ecosystem"],
}

_TEMPLATE_FILES = {
    "comparison": "comparison-template.html",
    "process": "process-breakdown-template.html",
    "data_bar": "data-viz-bar-template.html",
    "data_pie": "data-viz-pie-template.html",
    "data_curve": "data-viz-curve-template.html",
    "data_scatter": "data-viz-scatter-template.html",
    "carousel": "carousel-template.html",
    "resource_guide": "resource-guide-template.html",
}

_FORMAT_LABELS = {
    "comparison": "Comparison",
    "process": "Process Breakdown",
    "data_bar": "Bar Chart",
    "data_pie": "Pie / Donut Chart",
    "data_curve": "Trend Line Chart",
    "data_scatter": "Scatter Plot",
    "carousel": "Carousel",
    "resource_guide": "Resource Guide",
}


def _parse_image_placeholders(draft: str) -> list:
    """Extract all [IMAGE: description | visual hint] from the draft."""
    pattern = r'\[IMAGE:\s*([^\]|]+?)(?:\s*\|\s*([^\]]+?))?\s*\]'
    matches = re.findall(pattern, draft, re.IGNORECASE)
    results = []
    for i, (desc, hint) in enumerate(matches):
        results.append({
            "index": i,
            "description": desc.strip(),
            "hint": hint.strip() if hint else "",
            "placeholder": f"[IMAGE: {desc.strip()}{' | ' + hint.strip() if hint else ''}]",
        })
    return results


def _pick_format(description: str, hint: str) -> str:
    """Pick the best visual format based on description keywords. Haiku-free — pure heuristic."""
    combined = (description + " " + hint).lower()
    scores = {fmt: 0 for fmt in _FORMAT_KEYWORDS}
    for fmt, keywords in _FORMAT_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                scores[fmt] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "comparison"


def _read_design_system() -> str:
    path = config.VISUAL_SYSTEM_DIR / "DESIGN-SYSTEM.md"
    if path.exists():
        return path.read_text()
    return ""


def _read_template(format_type: str) -> str:
    filename = _TEMPLATE_FILES.get(format_type, "comparison-template.html")
    path = config.VISUAL_SYSTEM_DIR / filename
    if path.exists():
        return path.read_text()
    return ""


def _get_storylane_logo_b64() -> str:
    """Return base64-encoded Storylane SVG logo for embedding in visuals."""
    logo_path = config.VISUAL_SYSTEM_DIR / "logos" / "storylane_full_logo.svg"
    if logo_path.exists():
        return base64.b64encode(logo_path.read_bytes()).decode()
    return ""


def _generate_visual_html(placeholder: dict, article: dict, format_type: str) -> str:
    """Ask Claude Sonnet to generate a complete self-contained HTML visual."""
    design_system = _read_design_system()
    template_html = _read_template(format_type)
    logo_b64 = _get_storylane_logo_b64()
    logo_img_tag = f'<img src="data:image/svg+xml;base64,{logo_b64}" style="height:24px;" alt="Storylane">' if logo_b64 else "Storylane"

    topic = article.get("topic", "")
    angle = article.get("angle", "")

    prompt = f"""You are filling in content for a Storylane-branded HTML infographic template.

The template below is structurally complete with all CSS already written. Your ONLY job is to replace the example content with real, specific content for the requested visual. Do NOT change any CSS, class names, or structural HTML — only replace text nodes, numbers, labels, and `style="width:X%"` values inside existing elements.

## TEMPLATE (fill this in — return the COMPLETE modified HTML):
{template_html}

## WHAT TO FILL IN:
- **Visual description:** {placeholder['description']}
- **Article topic:** {topic}
- **Format:** {_FORMAT_LABELS[format_type]}

## RULES:
1. Return ONLY the complete HTML — no markdown fences, no explanation.
2. Replace ALL example content (titles, labels, rows, stats, chart values) with real content derived from the visual description and article topic.
3. Update the header H1 title and subtitle to match the visual subject.
4. Keep the badge text as: "{_FORMAT_LABELS[format_type]}"
5. Make italic+yellow `<em>` words relevant to this visual.
6. The CTA bar at the bottom: update text to be specific to this visual's topic. Keep the storylane.io link. Replace any placeholder logo with: {logo_img_tag}
7. For bar charts: update `style="width:X%"` values to match your real data.
8. For SVG charts: update path/circle/line coordinates to reflect real data points.
9. No lorem ipsum. All content must be real and relevant.

Return the complete modified HTML now:"""

    response = create_message("sonnet", max_tokens=6000, messages=[{"role": "user", "content": prompt}])
    html = response.content[0].text.strip()

    # Strip any accidental markdown code fences
    if html.startswith("```"):
        lines = html.split("\n")
        html = "\n".join(lines[1:])
        if html.rstrip().endswith("```"):
            html = "\n".join(html.rstrip().split("\n")[:-1])

    return html


def generate_images(article: dict, progress_cb=None) -> dict:
    """
    Parse IMAGE placeholders from the article draft and generate HTML visuals for each.
    Saves files to output/images/{article_id}/ and returns metadata.
    """
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    draft = article.get("draft", "")
    if not draft:
        return {"visuals": [], "count": 0, "error": "No draft found"}

    placeholders = _parse_image_placeholders(draft)
    if not placeholders:
        return {"visuals": [], "count": 0, "error": "No [IMAGE: ...] placeholders found in draft"}

    _p(f"Found {len(placeholders)} image placeholder(s) in draft...")

    article_id = article.get("id", "unknown")
    out_dir = config.IMAGES_OUTPUT_DIR / article_id
    out_dir.mkdir(parents=True, exist_ok=True)

    visuals = []
    for i, ph in enumerate(placeholders):
        _p(f"[{i+1}/{len(placeholders)}] Generating visual: {ph['description'][:50]}...")
        format_type = _pick_format(ph["description"], ph["hint"])
        try:
            html = _generate_visual_html(ph, article, format_type)
            filename = f"visual_{i+1:02d}_{format_type}.html"
            filepath = out_dir / filename
            filepath.write_text(html, encoding="utf-8")
            visuals.append({
                "index": i,
                "placeholder": ph["placeholder"],
                "description": ph["description"],
                "format": format_type,
                "format_label": _FORMAT_LABELS[format_type],
                "filename": filename,
                "filepath": str(filepath),
            })
            _p(f"[{i+1}/{len(placeholders)}] ✓ {filename}")
        except Exception as e:
            visuals.append({
                "index": i,
                "placeholder": ph["placeholder"],
                "description": ph["description"],
                "format": format_type,
                "format_label": _FORMAT_LABELS[format_type],
                "error": str(e),
            })
            _p(f"[{i+1}/{len(placeholders)}] Error: {e}")

    _p("DONE")
    return {
        "visuals": visuals,
        "count": len([v for v in visuals if not v.get("error")]),
        "errors": len([v for v in visuals if v.get("error")]),
        "output_dir": str(out_dir),
    }


def get_generated_visuals(article_id: str) -> list:
    """Return list of previously generated visuals for an article."""
    out_dir = config.IMAGES_OUTPUT_DIR / article_id
    if not out_dir.exists():
        return []
    visuals = []
    for f in sorted(out_dir.glob("visual_*.html")):
        visuals.append({
            "filename": f.name,
            "filepath": str(f),
            "size_kb": round(f.stat().st_size / 1024, 1),
        })
    return visuals
