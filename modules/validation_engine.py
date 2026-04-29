"""
Post-apply validation engine.

Runs AFTER QC or SEO suggestions are applied to an article.
Decoupled from the QC/SEO passes — doesn't know what suggestions were made,
just looks at the final article and asks three independent questions:

1. Insight fidelity    — did the customer voice survive the revision?
2. Meaning preservation — did the core thesis/ideal reader stay intact?
3. Keyword coherence   — do the selected keywords still match what this article is actually about?

This guards against the self-fulfilling loop where applying QC's own suggestions
always raises QC scores, even if the article drifted from its original intent.
"""

import json
from modules.model_manager import create_message


VALIDATION_PROMPT = """You are an independent content auditor. You have NOT seen any previous QC or SEO reports for this article. Your job is to assess whether the article still delivers on its original brief after revisions.

ORIGINAL BRIEF:
- Topic: {topic}
- Ideal reader: {ideal_reader}
- Angle: {angle}
- Why someone should read this: {why_read}
- Primary keyword: {primary_kw}
- Secondary keywords: {secondary_kws}

SOURCE INSIGHTS (these were the customer calls/data that inspired this topic):
{insights_block}

ARTICLE (post-revision):
{article}

Run three independent checks. Return a JSON object with:

"insight_fidelity": {{
  "score": integer 1-10,
  "status": "pass" | "warn" | "fail",
  "finding": "1-2 sentences on whether real customer language/quotes/pain points are still present and prominent",
  "suggestion": "specific fix if score < 7, else null"
}},

"meaning_preservation": {{
  "score": integer 1-10,
  "status": "pass" | "warn" | "fail",
  "finding": "1-2 sentences on whether the article still addresses the original topic and ideal reader — or if revisions shifted the focus",
  "suggestion": "specific fix if score < 7, else null"
}},

"keyword_coherence": {{
  "score": integer 1-10,
  "status": "pass" | "warn" | "fail",
  "finding": "1-2 sentences on whether the primary and secondary keywords are genuinely relevant to what the article is now actually about (not just mechanically inserted)",
  "suggestion": "specific fix if score < 7, else null"
}},

"overall_verdict": "pass" | "warn" | "fail",
"verdict_note": "1 sentence summary of the overall state"

Scoring guide: 8-10 = strong, 6-7 = minor concerns, below 6 = needs attention.
Return ONLY the JSON object."""


def run_validation(article: dict) -> dict:
    """
    Run post-apply validation on an article.
    article: full article dict with draft, topic, keywords, insight_ids_used, etc.
    """
    topic = article.get("topic", "")
    ideal_reader = article.get("ideal_reader", "")
    angle = article.get("angle", "")
    why_read = article.get("why_read", "")
    draft = article.get("draft", "")

    if not draft or not topic:
        return {"skipped": True, "reason": "No draft or topic to validate"}

    kw = article.get("keywords", {})
    primary_kw = (kw.get("primary") or {}).get("keyword", topic) if isinstance(kw.get("primary"), dict) else str(kw.get("primary", topic))
    raw_sec = kw.get("secondary", [])
    secondary_kws = ", ".join(k["keyword"] if isinstance(k, dict) else k for k in raw_sec[:5])

    # Load directive insights
    insight_ids = article.get("insight_ids_used") or []
    insights_block = "No source insights linked to this topic."
    if insight_ids:
        try:
            from modules.insight_extractor import get_insights_by_ids
            insights = get_insights_by_ids(insight_ids)
            if insights:
                parts = []
                for ins in insights[:5]:
                    quotes = [q["text"] if isinstance(q, dict) else str(q) for q in ins.get("quotes", [])[:2]]
                    pains = ins.get("pain_points", [])[:2]
                    src = ins.get("source_name") or ins.get("source_type", "")
                    entry = f"[{src}] Pains: {'; '.join(pains)}"
                    if quotes:
                        entry += f" | Quotes: {' | '.join(f'[{q[:80]}]' for q in quotes)}"
                    parts.append(entry)
                insights_block = "\n".join(parts)
        except Exception:
            pass

    prompt = VALIDATION_PROMPT.format(
        topic=topic,
        ideal_reader=ideal_reader or "Not specified",
        angle=angle or "Not specified",
        why_read=why_read or "Not specified",
        primary_kw=primary_kw,
        secondary_kws=secondary_kws or "None",
        insights_block=insights_block,
        article=draft[:12000],
    )

    response = create_message("sonnet", max_tokens=1500, messages=[{"role": "user", "content": prompt}])

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()

    return json.loads(raw)
