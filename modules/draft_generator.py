import json
import time
import config
from modules.company_brain import brief_as_prompt_context
from modules.insight_extractor import get_insights, get_insights_by_ids, mark_insight_used
from modules.demo_connector import find_best_demos
from modules.template_learner import get_format_context_for_angle, get_formats
from modules.link_library import get_relevant_links, format_links_for_prompt
from modules.model_manager import create_message
from modules.skills_manager import build_skills_block


def _select_relevant_insights(topic: str, tags: list, limit: int = 8) -> list:
    all_insights = get_insights({"min_confidence": 0.35, "limit": 100})
    topic_words = set(topic.lower().split())

    def relevance(ins):
        score = ins.get("confidence", 0.5)
        ins_tags = set(ins.get("tags", []))
        tag_overlap = len(ins_tags.intersection(set(tags)))
        score += tag_overlap * 0.3
        segment = (ins.get("customer_segment") or "").lower()
        summary = (ins.get("raw_summary") or "").lower()
        for word in topic_words:
            if len(word) > 4 and (word in segment or word in summary):
                score += 0.2
        return score

    scored = sorted(all_insights, key=relevance, reverse=True)
    return scored[:limit]


def _format_insights_for_prompt(insights: list, directive_ids: set = None) -> str:
    if not insights:
        return "No specific customer insights available for this topic."
    directive_ids = directive_ids or set()
    parts = []
    for ins in insights:
        def _str_item(item, *keys):
            """Safely coerce a possibly-dict list item to a string."""
            if not isinstance(item, dict):
                return str(item)
            for k in keys:
                if k in item:
                    return str(item[k])
            return str(item)

        pain = "; ".join(_str_item(p, "point", "pain", "text") for p in ins.get("pain_points", [])[:3])
        quotes = ins.get("quotes", [])
        all_quotes = []
        for q in quotes[:3]:
            text = q["text"] if isinstance(q, dict) else str(q)
            ctx = q.get("context", "") if isinstance(q, dict) else ""
            all_quotes.append(f'"{text}"' + (f" [{ctx}]" if ctx else ""))
        metrics = "; ".join(_str_item(m, "metric", "value", "text") for m in ins.get("metrics", [])[:3])
        use_cases = "; ".join(_str_item(u, "use_case", "case", "text") for u in ins.get("use_cases", [])[:2])
        segment = ins.get("customer_segment", "")
        source = ins.get("source_name") or ins.get("source_type", "")
        is_directive = ins["id"] in directive_ids
        header = f"★ DIRECTIVE INSIGHT [{ins['source_type']} / {source}]" if is_directive else f"[{ins['source_type']} / {segment} | {source}]"
        entry = f"{header}\n"
        entry += f"  Segment: {segment}\n" if segment else ""
        entry += f"  Pains: {pain}\n" if pain else ""
        entry += f"  Quotes: {' | '.join(all_quotes)}\n" if all_quotes else ""
        entry += f"  Metrics: {metrics}\n" if metrics else ""
        entry += f"  Use cases: {use_cases}" if use_cases else ""
        parts.append(entry.rstrip())
    return "\n\n".join(parts)


def generate_draft(article: dict, progress_cb=None) -> str:
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    topic = article.get("topic", "")
    ideal_reader = article.get("ideal_reader", "")
    angle = article.get("angle", "thought_leadership")
    pillar = article.get("pillar", "")
    strategic_intent = article.get("strategic_intent", [])
    keywords = article.get("keywords", {})
    primary_kw = keywords.get("primary", {})
    primary_kw_str = primary_kw.get("keyword", topic) if isinstance(primary_kw, dict) else str(primary_kw)
    secondary_kws = keywords.get("secondary", [])
    article_id = article.get("id", "")

    _p("Selecting relevant customer insights...")
    tags = []

    # First: load the directive insights linked at topic-generation time
    linked_ids = article.get("insight_ids_used") or []
    directive_insights = get_insights_by_ids(linked_ids) if linked_ids else []
    directive_id_set = {ins["id"] for ins in directive_insights}

    # Then: fill up with semantically relevant insights, deduping against directive ones
    semantic_insights = _select_relevant_insights(topic, tags, limit=8)
    semantic_insights = [ins for ins in semantic_insights if ins["id"] not in directive_id_set]

    # Directive insights go first so they appear prominently in the prompt
    insights = directive_insights + semantic_insights[:max(0, 8 - len(directive_insights))]
    insights_block = _format_insights_for_prompt(insights, directive_ids=directive_id_set)

    _p("Finding best-fit demos...")
    demos = find_best_demos(topic, angle, num=3)
    demos_block = ""
    if demos:
        demo_lines = []
        for d in demos:
            screenshot_note = f" [screenshot available at: {d['name']}/step_001]" if d.get("has_screenshot") else ""
            demo_lines.append(
                f"- {d['name']} | URL: {d['demo_url']} | Score: {d['overall_score']}/10{screenshot_note}\n"
                f"  Summary: {d['summary']}"
            )
        demos_block = "\n".join(demo_lines)
    else:
        demos_block = "No classified demos available. Use [DEMO: url | reason] placeholder if you reference demos."

    _p("Finding relevant internal links...")
    internal_links = get_relevant_links(topic, pillar, num=5)
    links_block = format_links_for_prompt(internal_links)

    _p("Loading content format context...")
    format_ctx = get_format_context_for_angle(angle)

    # Per-article structural brief — overrides / supplements angle template
    structural_brief = (article.get("structural_brief") or "").strip()
    if structural_brief:
        brief_block = f"""=== STRUCTURAL BRIEF FOR THIS ARTICLE ===
The editor has provided specific structural instructions for this article. Follow these closely — they take priority over the general format reference above.

{structural_brief}
=== END STRUCTURAL BRIEF ==="""
    else:
        brief_block = ""

    # Reusable writing instructions from Training Library (source_type = "instructions")
    raw_instructions = [f for f in get_formats() if f.get("source_type") == "instructions"]
    if raw_instructions:
        instr_lines = ["=== STANDING WRITING INSTRUCTIONS ===",
                       "These are always-on writing rules set by the editor. Apply them throughout the article.\n"]
        for instr in raw_instructions:
            instr_lines.append(f"— {instr.get('label', 'Instructions')} —")
            instr_lines.append(instr.get("content", ""))
            instr_lines.append("")
        instr_lines.append("=== END STANDING INSTRUCTIONS ===")
        instructions_block = "\n".join(instr_lines)
    else:
        instructions_block = ""

    # Draft skills from skills library
    draft_skills_block = build_skills_block("draft")

    company_ctx = brief_as_prompt_context()

    # Include manual keywords in the keyword instruction
    manual_kws = keywords.get("manual", [])
    sec_kw_strings = []
    for k in secondary_kws[:5]:
        sec_kw_strings.append(k.get("keyword", k) if isinstance(k, dict) else str(k))
    for k in manual_kws[:3]:
        if k not in sec_kw_strings:
            sec_kw_strings.append(k)

    kw_instruction = f"""PRIMARY KEYWORD: "{primary_kw_str}" — include in: H1 title, first 100 words, at least 2 H2 headings.
SECONDARY KEYWORDS: {', '.join(f'"{k}"' for k in sec_kw_strings)} — weave in naturally across the article.""" if primary_kw_str else ""

    strategic_notes = []
    if "eeat_signal" in strategic_intent:
        strategic_notes.append("Include at least one primary voice — a customer quote, founder perspective, or expert reference. No anonymous assertions.")
    if "backlinkable" in strategic_intent:
        strategic_notes.append("Frame at least one section as a standalone insight or data point that other publications would want to cite.")
    if "primary_voice" in strategic_intent:
        strategic_notes.append("Lead with or prominently feature a real customer voice. Use quotes from the insights provided.")
    strategic_block = "\n".join(f"- {n}" for n in strategic_notes) if strategic_notes else ""

    _p("Generating draft with Claude...")

    prompt = f"""{company_ctx}

{format_ctx}

{instructions_block + chr(10) if instructions_block else ""}{brief_block + chr(10) if brief_block else ""}{draft_skills_block + chr(10) if draft_skills_block else ""}You are an expert content writer for Storylane. Write a full, publication-quality article on the topic below.

TOPIC: {topic}
ANGLE: {angle}
PILLAR: {pillar}
IDEAL READER: {ideal_reader}

{kw_instruction}

STRATEGIC REQUIREMENTS:
{strategic_block if strategic_block else "- Write content that builds authority and earns the reader's trust."}

CUSTOMER INSIGHTS TO DRAW FROM:
{insights_block}
{f"""
DIRECTIVE: The insights marked ★ above are the PRIMARY SOURCES that informed this topic. You must weave their specific language, quotes, and pain points into the article. At minimum, use one direct or near-direct quote from a ★ insight. Don't paraphrase away the raw customer voice — that's what makes this content authentic.""" if directive_insights else ""}

AVAILABLE DEMOS (use these for placeholders):
{demos_block}

{links_block + chr(10) if links_block else ""}WRITING RULES — NON-NEGOTIABLE:
1. Start immediately with the substance. Zero preamble. The first sentence must pull the reader in.
2. No filler sentences. Every sentence must earn its place. If you can remove it without losing meaning, cut it.
3. No generic opener like "In today's B2B landscape..." or "Many companies struggle with..."
4. Skimmable structure: short paragraphs (2-3 sentences max), descriptive H2/H3 headings, use bullet points for lists.
5. EEAT baked in: cite specific companies, specific metrics, real scenarios from the insights above.
6. Not bloated: aim for 900-1400 words depending on angle. Listicles can go longer if each item is tight.
7. STORYLANE PRESENCE — MANDATORY: Storylane must appear as a genuine, substantive part of this article — not a sponsor note or closing footnote. These topics come from Storylane's own customer insights, which means the product's perspective, capabilities, or approach must be woven into the core argument. Think of how Ahrefs writes about SEO or SparkToro writes about audience research: they're always the natural protagonist of their own content, present throughout, not shoe-horned at the end. Since you have the company brief and customer insights, you already have everything needed to make this organic.
8. CLOSING CTA — MANDATORY: End with a dedicated H2 section that earns its place. Connect the article's specific insight back to what Storylane does about it. Make it feel like a natural conclusion, not an ad. Include a clear call-to-action to start a free trial at storylane.io. The section should be 2-3 sentences max — tight, specific, and earned by the article above it.

PLACEHOLDER SYNTAX — use exactly as shown:
- For images: [IMAGE: description of what should go here | visual suggestion]
- For interactive demos: [DEMO: {demos[0]['demo_url'] if demos else 'demo-url'} | reason this demo fits here]
- For screenshots: [SCREENSHOT: {demos[0]['name'] if demos else 'CompanyName'}/step_001 | what this screenshot shows]
- Use demo URLs and company names from the available demos list above when relevant.

FORMAT: Return the article in clean Markdown. H1 for title, H2/H3 for sections. No meta commentary."""

    response = create_message("sonnet", max_tokens=4000, messages=[{"role": "user", "content": prompt}])

    draft = response.content[0].text.strip()

    # Mark all referenced insights as used (directive + semantic)
    all_ids = list(directive_id_set) + [ins["id"] for ins in semantic_insights[:max(0, 8 - len(directive_insights))]]
    for ins_id in all_ids:
        mark_insight_used(ins_id, article_id)

    _p("Draft complete.")
    return draft
