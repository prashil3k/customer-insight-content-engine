import json
import re
import config
from modules.model_manager import create_message
from modules.link_library import get_relevant_links


def _count_keyword_occurrences(text: str, keyword: str) -> int:
    return len(re.findall(re.escape(keyword.lower()), text.lower()))


def _structural_checks(article: str, primary_kw: str, secondary_kws: list, topic: str = "", pillar: str = "") -> list:
    issues = []
    lines = article.split("\n")
    h1_lines = [l for l in lines if l.startswith("# ") and not l.startswith("## ")]
    h2_lines = [l for l in lines if l.startswith("## ")]
    h3_lines = [l for l in lines if l.startswith("### ")]

    kw_lower = primary_kw.lower()
    article_lower = article.lower()
    words = re.findall(r'\b\w+\b', article_lower)
    total_words = len(words)
    kw_count = _count_keyword_occurrences(article, primary_kw)
    kw_density = (kw_count / total_words * 100) if total_words > 0 else 0

    first_100 = " ".join(article_lower.split()[:100])
    first_para = "\n".join(lines[:5]).lower()
    h1_text = h1_lines[0].lower() if h1_lines else ""
    h2_text_all = " ".join(h2_lines).lower()

    if not h1_lines:
        issues.append({"check": "H1 missing", "severity": "critical", "location": "Document structure",
                       "issue": "No H1 found. Every article needs exactly one H1 title.",
                       "suggestion": "Add an H1 title (# Title) at the top of the article."})
    elif kw_lower not in h1_text:
        issues.append({"check": "Primary KW in H1", "severity": "major", "location": "H1 title",
                       "issue": f'Primary keyword "{primary_kw}" not found in H1 title.',
                       "suggestion": f'Include "{primary_kw}" naturally in the H1 title.'})

    if kw_lower not in first_100:
        issues.append({"check": "Primary KW in intro", "severity": "major", "location": "First 100 words",
                       "issue": f'Primary keyword "{primary_kw}" missing from the first 100 words.',
                       "suggestion": f'Work "{primary_kw}" into the opening paragraph naturally.'})

    kw_in_h2_count = sum(1 for h2 in h2_lines if kw_lower in h2.lower())
    if kw_in_h2_count < 2:
        issues.append({"check": "Primary KW in H2s", "severity": "major", "location": "H2 headings",
                       "issue": f'Primary keyword only appears in {kw_in_h2_count} H2(s). Need at least 2.',
                       "suggestion": f'Rework 1-2 H2 headings to include "{primary_kw}" naturally.'})

    if kw_density < 0.5:
        issues.append({"check": "Keyword density low", "severity": "minor", "location": "Full article",
                       "issue": f'Keyword density is {kw_density:.2f}% (target: 0.5-1.5%). Keyword appears {kw_count}x in ~{total_words} words.',
                       "suggestion": f'Add "{primary_kw}" 2-3 more times in natural context.'})
    elif kw_density > 2.0:
        issues.append({"check": "Keyword stuffing", "severity": "major", "location": "Full article",
                       "issue": f'Keyword density is {kw_density:.2f}% — too high, looks spammy.',
                       "suggestion": "Replace some keyword instances with natural synonyms or rephrase."})

    if total_words < 700:
        issues.append({"check": "Content length", "severity": "major", "location": "Full article",
                       "issue": f"Article is only ~{total_words} words. Most competitive SERPs require 900+ for this type of content.",
                       "suggestion": "Expand 1-2 sections with more specific detail, examples, or a new sub-section."})

    # Internal link check — use the link library if populated, otherwise generic check
    has_any_link = any(re.search(r'https?://', l) for l in lines)
    relevant_lib_links = get_relevant_links(topic or primary_kw, pillar, num=4)
    if relevant_lib_links:
        # Check which indexed links are actually in the article
        used = [l for l in relevant_lib_links if l["url"] in article]
        missing = [l for l in relevant_lib_links if l["url"] not in article]
        if not used:
            link_suggestions = "; ".join(f'"{l["title"]}" ({l["url"]})' for l in missing[:2])
            issues.append({"check": "Internal links", "severity": "minor", "location": "Full article",
                           "issue": f"No indexed internal links used. {len(relevant_lib_links)} relevant Storylane pages are available to link to.",
                           "suggestion": f"Add links to: {link_suggestions}"})
        elif len(used) < 2 and missing:
            link_suggestions = "; ".join(f'"{l["title"]}" ({l["url"]})' for l in missing[:2])
            issues.append({"check": "Internal links", "severity": "minor", "location": "Full article",
                           "issue": f"Only {len(used)} internal link(s) used. More relevant pages are available.",
                           "suggestion": f"Consider also linking to: {link_suggestions}"})
    elif not has_any_link:
        issues.append({"check": "Internal links", "severity": "minor", "location": "Full article",
                       "issue": "No links found. Internal links help distribute authority and aid navigation.",
                       "suggestion": "Add 2-3 internal links to related Storylane content or pages. Add URLs to Settings → Internal Link Library so future drafts include them automatically."})

    for sec_kw in secondary_kws[:5]:
        if sec_kw.lower() not in article_lower:
            issues.append({"check": f"Secondary KW missing: {sec_kw}", "severity": "minor", "location": "Full article",
                           "issue": f'Secondary keyword "{sec_kw}" not found in article.',
                           "suggestion": f'Work "{sec_kw}" into a relevant paragraph or heading naturally.'})

    return issues


def run_seo(article_content: str, keywords: dict = None, topic: str = "", pillar: str = "", progress_cb=None) -> dict:
    if progress_cb:
        progress_cb("Running structural SEO checks...")

    keywords = keywords or {}
    primary_kw_data = keywords.get("primary", {})
    primary_kw = primary_kw_data.get("keyword", topic) if isinstance(primary_kw_data, dict) else str(primary_kw_data)
    # secondary can be list of objects (new format) or strings (legacy)
    raw_secondary = keywords.get("secondary", [])
    secondary_kws = [k["keyword"] if isinstance(k, dict) else k for k in raw_secondary]

    structural = _structural_checks(article_content, primary_kw, secondary_kws, topic=topic, pillar=pillar)

    if progress_cb:
        progress_cb("Running semantic SEO check with Claude...")

    prompt = f"""You are an SEO specialist doing a Surfer SEO-style semantic content audit.

PRIMARY KEYWORD: {primary_kw}
SECONDARY KEYWORDS: {', '.join(secondary_kws[:5])}

ARTICLE:
{article_content[:12000]}

Check for:
1. Semantic keyword gaps — what related terms/phrases would a comprehensive article on this topic include that are missing?
2. Meta title suggestion — write an optimised meta title (50-60 chars, includes primary keyword)
3. Meta description suggestion — write an optimised meta description (140-155 chars, compelling, includes primary keyword)
4. Missing sub-topics — what angles or sub-questions does a searcher for "{primary_kw}" expect answered that aren't covered?
5. Content structure — is the heading hierarchy logical? Any H2 that should be split or merged?

Return a JSON object with:
- "meta_title": Suggested meta title string
- "meta_description": Suggested meta description string
- "semantic_gaps": Array of missing semantic terms/phrases with "term" and "where_to_add"
- "missing_subtopics": Array of missing sub-topics with "subtopic" and "suggested_location"
- "structure_notes": Array of structural suggestions (heading changes, section additions)
- "semantic_score": Integer 1-10 (how semantically complete is this article?)

Return ONLY the JSON object."""

    custom = config.load_settings().get("seo_rubric", "").strip()
    if custom:
        prompt += f"\n\nADDITIONAL SEO FOCUS:\n{custom}"

    response = create_message("sonnet", max_tokens=4000, messages=[{"role": "user", "content": prompt}])

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()
    semantic = json.loads(raw)

    all_suggestions = []
    for i, s in enumerate(structural):
        all_suggestions.append({
            "id": i + 1,
            "type": "structural",
            "severity": s["severity"],
            "location": s["location"],
            "issue": s["issue"],
            "suggestion": s["suggestion"],
            "check": s["check"],
        })

    offset = len(all_suggestions)
    for i, gap in enumerate(semantic.get("semantic_gaps", [])):
        all_suggestions.append({
            "id": offset + i + 1,
            "type": "semantic",
            "severity": "minor",
            "location": gap.get("where_to_add", "Relevant section"),
            "issue": f"Semantic keyword missing: \"{gap.get('term', '')}\"",
            "suggestion": f"Add \"{gap.get('term', '')}\" in {gap.get('where_to_add', 'a relevant section')}.",
        })

    offset = len(all_suggestions)
    for i, sub in enumerate(semantic.get("missing_subtopics", [])):
        all_suggestions.append({
            "id": offset + i + 1,
            "type": "subtopic",
            "severity": "major",
            "location": sub.get("suggested_location", "New section"),
            "issue": f"Missing sub-topic: {sub.get('subtopic', '')}",
            "suggestion": f"Add a section covering: {sub.get('subtopic', '')}",
        })

    if progress_cb:
        progress_cb("SEO check complete.")

    return {
        "suggestions": all_suggestions,
        "meta_title": semantic.get("meta_title", ""),
        "meta_description": semantic.get("meta_description", ""),
        "semantic_score": semantic.get("semantic_score", 5),
        "structural_issue_count": len(structural),
        "total_suggestions": len(all_suggestions),
    }


def apply_seo_suggestions(article_content: str, suggestions: list, meta: dict = None, progress_cb=None) -> str:
    if not suggestions:
        return article_content

    if progress_cb:
        progress_cb("Applying SEO suggestions...")

    suggestions_block = "\n".join(
        f"{i+1}. [{s.get('type','').upper()}] At: {s.get('location','')} | Issue: {s.get('issue','')} | Fix: {s.get('suggestion','')}"
        for i, s in enumerate(suggestions)
    )

    prompt = f"""Apply the following SEO improvements to this article. Make targeted changes only — do not rewrite sections that don't need changes.

ARTICLE:
{article_content}

SEO CHANGES TO APPLY:
{suggestions_block}

Return the full revised article in clean Markdown. No commentary."""

    response = create_message("sonnet", max_tokens=5000, messages=[{"role": "user", "content": prompt}])
    if progress_cb:
        progress_cb("SEO suggestions applied.")
    return response.content[0].text.strip()
