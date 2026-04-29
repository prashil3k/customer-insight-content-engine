import json
import config
from modules.model_manager import create_message


QC_PROMPT = """You are a ruthless content editor. Your job is to make this article publication-quality — the kind of thing that would run in a serious B2B publication or Substack. You have zero tolerance for:
- Preamble (any wind-up before the actual content starts)
- Filler sentences (sentences that say nothing)
- Rambling (paragraphs that meander)
- Generic claims (assertions without proof or specificity)
- Weak hooks (an opener that wouldn't make a busy person keep reading)

ARTICLE:
{article}

ARTICLE TOPIC: {topic}
IDEAL READER: {ideal_reader}
{insights_block}
Run a QC check. For each issue found, return a suggestion object. Be specific — don't say "improve the hook", say exactly what's wrong and what to do.

Return a JSON object with:
- "overall_score": Integer 1-10 (10 = publication-ready, 7 = good with minor fixes, below 5 = needs significant work)
- "hook_score": Integer 1-10 (does the opening pull the reader in immediately?)
- "fluff_score": Integer 1-10 (10 = zero fluff, 1 = very fluffy)
- "relevancy_score": Integer 1-10 (every section earns its place)
- "reader_value_score": Integer 1-10 (would the ideal reader give a damn?)
- "eeat_score": Integer 1-10 (specific voices, data, proof — not just assertions)
- "insight_authenticity_score": Integer 1-10 (10 = article is clearly grounded in real customer voices from the source insights; 1 = completely generic, no real data or quotes used){insight_dimension_note}
- "summary": 2-3 sentences on the overall state of this article
- "suggestions": Array of suggestion objects, each with:
    - "id": sequential integer starting at 1
    - "type": "hook" | "fluff" | "structure" | "eeat" | "relevancy" | "cta" | "preamble" | "rambling" | "insight_gap"
    - "severity": "critical" | "major" | "minor"
    - "location": Description of where in the article (e.g. "Opening paragraph", "Under H2: PreSales Crisis", "Final CTA")
    - "issue": What's wrong (specific)
    - "suggestion": What to do about it (specific and actionable)
    - "example_fix": If helpful, show a concrete rewrite of the problematic text (keep it short)

Return ONLY the JSON object. No preamble."""


def _format_directive_insights(insights: list) -> tuple[str, str]:
    """Returns (insights_block for prompt, insight_dimension_note for score description)."""
    if not insights:
        return "", ""
    parts = []
    for ins in insights:
        quotes = ins.get("quotes", [])
        quote_texts = [q["text"] if isinstance(q, dict) else str(q) for q in quotes[:2]]
        def _s(item, *keys):
            if not isinstance(item, dict):
                return str(item)
            for k in keys:
                if k in item:
                    return str(item[k])
            return str(item)

        pains = [_s(p, "point", "pain", "text") for p in ins.get("pain_points", [])[:2]]
        metrics = [_s(m, "metric", "value", "text") for m in ins.get("metrics", [])[:2]]
        source = ins.get("source_name") or ins.get("source_type", "")
        entry = f"SOURCE: {source} ({ins.get('customer_segment','')})"
        if quote_texts:
            entry += f"\n  Quotes available: {' | '.join(f'[{q[:100]}]' for q in quote_texts)}"
        if pains:
            entry += f"\n  Pain points: {'; '.join(pains)}"
        if metrics:
            entry += f"\n  Metrics: {'; '.join(metrics)}"
        parts.append(entry)

    block = "\nSOURCE INSIGHTS (these should be evident in the article — check for their presence):\n" + "\n\n".join(parts) + "\n"
    note = " — check whether the source insights above are actually referenced in this article; flag as insight_gap if they aren't"
    return block, note


def run_qc(article_content: str, topic: str = "", ideal_reader: str = "", directive_insights: list = None, progress_cb=None) -> dict:
    if progress_cb:
        progress_cb("Running quality check...")

    insights_block, insight_dimension_note = _format_directive_insights(directive_insights or [])
    custom = config.load_settings().get("qc_rubric", "").strip()
    prompt = QC_PROMPT.format(
        article=article_content[:15000],
        topic=topic,
        ideal_reader=ideal_reader,
        insights_block=insights_block,
        insight_dimension_note=insight_dimension_note,
    )
    if custom:
        prompt += f"\n\nADDITIONAL RUBRIC (apply these on top of the above):\n{custom}"

    response = create_message("sonnet", max_tokens=5000, messages=[{"role": "user", "content": prompt}])

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()

    result = json.loads(raw)
    if progress_cb:
        progress_cb("QC complete.")
    return result


def apply_qc_suggestions(article_content: str, suggestions: list, topic: str = "", progress_cb=None) -> str:
    if not suggestions:
        return article_content

    if progress_cb:
        progress_cb("Applying QC suggestions...")

    suggestions_block = "\n".join(
        f"{i+1}. [{s.get('type','').upper()}] At: {s.get('location','')} | Issue: {s.get('issue','')} | Fix: {s.get('suggestion','')} | Example: {s.get('example_fix','')}"
        for i, s in enumerate(suggestions)
    )

    prompt = f"""Apply the following editorial suggestions to this article. Make ONLY the changes described — do not rewrite sections that aren't mentioned, do not change the overall structure unless specified.

ARTICLE:
{article_content}

SUGGESTIONS TO APPLY:
{suggestions_block}

Return the full revised article in clean Markdown. No commentary, no explanation — just the revised article."""

    response = create_message("sonnet", max_tokens=5000, messages=[{"role": "user", "content": prompt}])
    if progress_cb:
        progress_cb("Suggestions applied.")
    return response.content[0].text.strip()
