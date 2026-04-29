import io
import json
import os
import time
import threading
import zipfile
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, send_file, abort
import config
import scheduler as sched

app = Flask(__name__, static_folder=str(config.STATIC_DIR))
_progress = {}


def _set_progress(task_id: str, msg: str):
    _progress[task_id] = {"msg": msg, "ts": time.time()}


def _get_settings() -> dict:
    if config.SETTINGS_PATH.exists():
        d = json.loads(config.SETTINGS_PATH.read_text())
        return {**config.DEFAULT_SETTINGS, **d}
    return config.DEFAULT_SETTINGS.copy()


def _save_settings(data: dict):
    config.SETTINGS_PATH.write_text(json.dumps(data, indent=2))


# ── Static ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(config.STATIC_DIR), "index.html")


# ── Status ─────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    from modules.insight_extractor import get_insight_stats
    from modules.demo_connector import get_demo_stats
    from modules.topic_planner import get_articles
    stats = get_insight_stats()
    demo_stats = get_demo_stats()
    articles = get_articles()
    return jsonify({
        "scheduler": sched.status(),
        "insights": stats,
        "demos": demo_stats,
        "articles_total": len(articles),
        "articles_by_stage": {s: sum(1 for a in articles if a.get("stage") == s)
                               for s in ["idea", "keywords", "draft", "qc", "seo", "done"]},
        "company_brief_exists": config.COMPANY_BRIEF_PATH.exists(),
        "scheduler_log": sched.get_log()[-20:],
    })


@app.route("/api/progress/<task_id>")
def api_progress(task_id):
    p = _progress.get(task_id, {})
    return jsonify(p)


# ── Company Brain ───────────────────────────────────────────────────────────────

@app.route("/api/company-brief")
def api_get_brief():
    from modules.company_brain import get_company_brief
    return jsonify(get_company_brief())


@app.route("/api/company-brain/scan", methods=["POST"])
def api_scan_company():
    body = request.json or {}
    urls = body.get("urls", [])
    raw_docs = body.get("raw_docs", [])
    task_id = f"scan_{int(time.time())}"

    if not config.ANTHROPIC_API_KEY:
        return jsonify({"error": "Anthropic API key not set. Go to Settings → API Keys first."}), 400

    def _run():
        from modules.company_brain import scan_company_intelligence
        try:
            _set_progress(task_id, "Starting company intelligence scan...")
            scan_company_intelligence(urls, raw_docs, progress_cb=lambda m: _set_progress(task_id, m))
            _set_progress(task_id, "DONE")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/company-brain/upload", methods=["POST"])
def api_upload_brain_file():
    import tempfile, os
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    suffix = "." + f.filename.rsplit(".", 1)[-1] if "." in f.filename else ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name
    try:
        from modules.company_brain import extract_text_from_file
        text = extract_text_from_file(tmp_path, f.filename)
    finally:
        os.unlink(tmp_path)
    return jsonify({"filename": f.filename, "text": text, "chars": len(text)})


# ── Sybill Webhook ─────────────────────────────────────────────────────────────

@app.route("/api/sybill/webhook", methods=["POST"])
def api_sybill_webhook():
    from modules.sybill_connector import process_sybill_webhook, verify_svix_signature
    payload_bytes = request.get_data()
    payload = request.json or {}

    # Optional signature verification — only if token/secret is set
    if config.SYBILL_API_TOKEN:
        if not verify_svix_signature(payload_bytes, dict(request.headers), config.SYBILL_API_TOKEN):
            return jsonify({"error": "Invalid signature"}), 401

    def _run():
        process_sybill_webhook(payload, progress_cb=lambda m: None)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True}), 200


# ── Insights ────────────────────────────────────────────────────────────────────

@app.route("/api/insights")
def api_get_insights():
    from modules.insight_extractor import get_insights
    filters = {k: v for k, v in request.args.items()}
    return jsonify(get_insights(filters))


@app.route("/api/insights/by_ids", methods=["POST"])
def api_get_insights_by_ids():
    from modules.insight_extractor import get_insights_by_ids
    body = request.json or {}
    ids = body.get("ids", [])
    if not ids:
        return jsonify([])
    return jsonify(get_insights_by_ids(ids))


@app.route("/api/insights/dump", methods=["POST"])
def api_add_dump():
    body = request.json or {}
    text = body.get("text", "").strip()
    author = body.get("author", "team")
    if not text:
        return jsonify({"error": "text required"}), 400
    task_id = f"dump_{int(time.time())}"

    def _run():
        from modules.insight_extractor import add_thought_dump
        try:
            _set_progress(task_id, "Extracting insights from thought dump...")
            result = add_thought_dump(text, author)
            _set_progress(task_id, "DONE")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/insights/upload", methods=["POST"])
def api_upload_transcript():
    body = request.json or {}
    text = body.get("text", "").strip()
    source_name = body.get("source_name", "uploaded_transcript")
    source_type = body.get("source_type", "manual")
    author = body.get("author", "")
    if not text:
        return jsonify({"error": "text required"}), 400
    task_id = f"upload_{int(time.time())}"

    def _run():
        from modules.insight_extractor import extract_insights_from_text
        import uuid
        try:
            _set_progress(task_id, f"Processing {source_name}...")
            extract_insights_from_text(text, {
                "source_id": f"upload_{uuid.uuid4().hex}",
                "source_type": source_type,
                "source_name": source_name,
                "author": author,
            })
            _set_progress(task_id, "DONE")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/watch/process", methods=["POST"])
def api_process_watch():
    task_id = f"watch_{int(time.time())}"

    def _run():
        from modules.insight_extractor import process_watch_folder
        try:
            _set_progress(task_id, "Scanning watch folders...")
            results = process_watch_folder(progress_cb=lambda m: _set_progress(task_id, m))
            _set_progress(task_id, f"DONE — {len(results)} insights extracted")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/watch/files")
def api_watch_files():
    files = []
    for folder in ["grain", "sybill"]:
        path = config.WATCH_DIR / folder
        for f in path.iterdir():
            files.append({"name": f.name, "folder": folder, "size": f.stat().st_size})
    return jsonify(files)


# ── Content Pillars ─────────────────────────────────────────────────────────────

@app.route("/api/pillars")
def api_get_pillars():
    from modules.pillar_map import get_pillar_map, get_pillar_gaps
    return jsonify({"map": get_pillar_map(), "gaps": get_pillar_gaps()})


@app.route("/api/pillars/add", methods=["POST"])
def api_add_pillar():
    body = request.json or {}
    from modules.pillar_map import add_pillar
    add_pillar(body.get("name", ""), body.get("description", ""))
    return jsonify({"ok": True})


# ── Topics & Articles ───────────────────────────────────────────────────────────

@app.route("/api/topics/generate", methods=["POST"])
def api_generate_topics():
    body = request.json or {}
    num = int(body.get("num", 5))
    task_id = f"topics_{int(time.time())}"

    def _run():
        from modules.topic_planner import generate_topics
        try:
            _set_progress(task_id, "Generating topic proposals...")
            generate_topics(num_topics=num, progress_cb=lambda m: _set_progress(task_id, m))
            _set_progress(task_id, "DONE")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/articles")
def api_get_articles():
    from modules.topic_planner import get_articles
    stage = request.args.get("stage")
    return jsonify(get_articles(stage))


@app.route("/api/articles", methods=["POST"])
def api_create_article():
    body = request.json or {}
    from modules.topic_planner import create_article_manually
    article = create_article_manually(
        body.get("topic", "New Article"),
        body.get("angle", "thought_leadership"),
        body.get("pillar", ""),
    )
    return jsonify(article)


@app.route("/api/articles/<article_id>")
def api_get_article(article_id):
    from modules.topic_planner import get_article
    a = get_article(article_id)
    if not a:
        abort(404)
    return jsonify(a)


@app.route("/api/articles/<article_id>", methods=["PUT"])
def api_update_article(article_id):
    from modules.topic_planner import get_article, save_article
    a = get_article(article_id)
    if not a:
        abort(404)
    body = request.json or {}
    a.update(body)
    a["id"] = article_id
    return jsonify(save_article(a))


@app.route("/api/articles/<article_id>/draft", methods=["PUT"])
def api_save_draft(article_id):
    from modules.topic_planner import get_article, save_article
    a = get_article(article_id)
    if not a:
        abort(404)
    body = request.json or {}
    draft = body.get("draft", "")
    if draft:
        a["draft"] = draft
        save_article(a)
    return jsonify({"ok": True})


@app.route("/api/articles/<article_id>/keywords", methods=["POST"])
def api_research_keywords(article_id):
    from modules.topic_planner import get_article, save_article
    a = get_article(article_id)
    if not a:
        abort(404)
    task_id = f"kw_{article_id}"

    def _run():
        from modules.keyword_researcher import research_keywords
        try:
            _set_progress(task_id, "Researching keywords...")
            # Preserve any manually entered keywords across research runs
            manual_kws = (a.get("keywords") or {}).get("manual", []) or None
            result = research_keywords(
                a["topic"], a.get("ideal_reader", ""), a.get("angle", ""),
                manual_keywords=manual_kws,
                progress_cb=lambda m: _set_progress(task_id, m),
            )
            # Keep manual keywords in the result so UI can show them
            if manual_kws:
                result["manual"] = manual_kws
            a["keywords"] = result
            a["stage"] = "keywords"
            save_article(a)
            _set_progress(task_id, "DONE")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/articles/<article_id>/draft", methods=["POST"])
def api_generate_draft(article_id):
    from modules.topic_planner import get_article, save_article
    a = get_article(article_id)
    if not a:
        abort(404)
    task_id = f"draft_{article_id}"

    def _run():
        from modules.draft_generator import generate_draft
        try:
            _set_progress(task_id, "Generating draft...")
            draft = generate_draft(a, progress_cb=lambda m: _set_progress(task_id, m))
            a["draft"] = draft
            a["stage"] = "draft"
            save_article(a)
            _set_progress(task_id, "DONE")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/articles/<article_id>/qc", methods=["POST"])
def api_run_qc(article_id):
    from modules.topic_planner import get_article, save_article
    a = get_article(article_id)
    if not a:
        abort(404)
    task_id = f"qc_{article_id}"

    def _run():
        from modules.qc_engine import run_qc, apply_qc_suggestions
        from modules.topic_planner import get_article as _ga, save_article as _sa
        from modules.insight_extractor import get_insights_by_ids as _gbi
        try:
            _set_progress(task_id, "Running QC check...")
            current = _ga(article_id)
            linked_ids = current.get("insight_ids_used") or []
            directive_insights = _gbi(linked_ids) if linked_ids else []
            result = run_qc(
                current.get("draft", ""),
                current.get("topic", ""),
                current.get("ideal_reader", ""),
                directive_insights=directive_insights,
                progress_cb=lambda m: _set_progress(task_id, m),
            )
            current["qc_result"] = result
            current["stage"] = "qc"

            settings = _get_settings()
            if settings.get("auto_apply", {}).get("qc"):
                _set_progress(task_id, "Auto-applying QC suggestions...")
                critical = [s for s in result.get("suggestions", []) if s.get("severity") in ("critical", "major")]
                if critical:
                    current["draft"] = apply_qc_suggestions(current["draft"], critical)

            _sa(current)
            _set_progress(task_id, "DONE")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/articles/<article_id>/seo", methods=["POST"])
def api_run_seo(article_id):
    from modules.topic_planner import get_article, save_article
    a = get_article(article_id)
    if not a:
        abort(404)
    task_id = f"seo_{article_id}"

    def _run():
        from modules.seo_engine import run_seo, apply_seo_suggestions
        from modules.topic_planner import get_article as _ga, save_article as _sa
        try:
            _set_progress(task_id, "Running SEO check...")
            current = _ga(article_id)
            result = run_seo(
                current.get("draft", ""),
                current.get("keywords", {}),
                current.get("topic", ""),
                pillar=current.get("pillar", ""),
                progress_cb=lambda m: _set_progress(task_id, m),
            )
            current["seo_result"] = result
            current["stage"] = "seo"

            settings = _get_settings()
            if settings.get("auto_apply", {}).get("seo"):
                _set_progress(task_id, "Auto-applying SEO suggestions...")
                critical = [s for s in result.get("suggestions", []) if s.get("severity") in ("critical", "major")]
                if critical:
                    current["draft"] = apply_seo_suggestions(current["draft"], critical)

            _sa(current)
            _set_progress(task_id, "DONE")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/articles/<article_id>/keywords/manual", methods=["PUT"])
def api_save_manual_keywords(article_id):
    from modules.topic_planner import get_article, save_article
    a = get_article(article_id)
    if not a:
        abort(404)
    body = request.json or {}
    raw = body.get("keywords", "")
    if isinstance(raw, list):
        kw_list = [k.strip() for k in raw if str(k).strip()]
    else:
        # Accept newline or comma separated
        kw_list = [k.strip() for line in str(raw).split("\n") for k in line.split(",") if k.strip()]
    kws = a.get("keywords") or {}
    if not isinstance(kws, dict):
        kws = {}
    kws["manual"] = kw_list
    a["keywords"] = kws
    save_article(a)
    return jsonify({"ok": True, "count": len(kw_list)})


@app.route("/api/articles/<article_id>/insight-check", methods=["POST"])
def api_insight_check(article_id):
    from modules.topic_planner import get_article
    from modules.insight_extractor import get_insights_by_ids
    a = get_article(article_id)
    if not a:
        abort(404)
    draft = a.get("draft", "")
    if not draft:
        return jsonify({"ok": True, "issues": [], "skipped": "no draft"})
    insight_ids = a.get("insight_ids_used", [])
    if not insight_ids:
        return jsonify({"ok": True, "issues": [], "skipped": "no linked insights"})
    insights = get_insights_by_ids(insight_ids)
    if not insights:
        return jsonify({"ok": True, "issues": [], "skipped": "insights not found"})

    # Collect checkable elements: substantial quotes + specific metrics
    elements = []
    for ins in insights:
        for q in ins.get("quotes", [])[:2]:
            text = q["text"] if isinstance(q, dict) else str(q)
            if len(text.split()) >= 6:
                elements.append({"type": "quote", "text": text[:200]})
        for m in ins.get("metrics", [])[:2]:
            if m and len(str(m).strip()) > 4:
                elements.append({"type": "metric", "text": str(m)[:120]})
    if not elements:
        return jsonify({"ok": True, "issues": [], "skipped": "no quotes or metrics to check"})

    elements_block = "\n".join(f"{i+1}. [{e['type'].upper()}] {e['text']}" for i, e in enumerate(elements))
    prompt = f"""You are checking whether specific customer insight elements were preserved in a final article.

SOURCE ELEMENTS (quotes and metrics from the original customer calls):
{elements_block}

ARTICLE (first 3500 chars):
{draft[:3500]}

For each numbered element, determine its status:
- "present" — the quote or metric appears (exact, near-exact, or clearly referenced with the specific number/language)
- "paraphrased" — the meaning is there but the specific wording or number has been genericised
- "missing" — not represented at all

Return a JSON array of ONLY elements with status "paraphrased" or "missing":
[{{"index": 1, "status": "paraphrased|missing", "note": "one sentence explanation"}}]
If everything is present, return []. Return ONLY the JSON array."""

    try:
        from modules.model_manager import create_message as _cm
        resp = _cm("haiku", max_tokens=600, messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("```").strip()
        issues_raw = json.loads(raw)
        issues = []
        for issue in issues_raw:
            idx = issue.get("index", 0) - 1
            if 0 <= idx < len(elements):
                issues.append({
                    "type": elements[idx]["type"],
                    "text": elements[idx]["text"],
                    "status": issue["status"],
                    "note": issue.get("note", ""),
                })
        return jsonify({"ok": len(issues) == 0, "issues": issues})
    except Exception as e:
        return jsonify({"ok": True, "issues": [], "skipped": f"check unavailable: {e}"})


@app.route("/api/pipeline/process-all", methods=["POST"])
def api_process_all():
    from modules.topic_planner import get_articles, get_article as _ga, save_article as _sa
    body = request.json or {}
    from_stage = body.get("stage", "idea")
    articles = get_articles(stage=from_stage)
    if not articles:
        return jsonify({"task_id": None, "queued": 0, "message": f"No articles at '{from_stage}' stage"})
    task_id = f"process_all_{int(time.time())}"
    settings = _get_settings()
    auto_advance = settings.get("auto_advance", False)
    auto_apply_qc = settings.get("auto_apply", {}).get("qc", False)
    auto_apply_seo = settings.get("auto_apply", {}).get("seo", False)

    def _run():
        from modules.keyword_researcher import research_keywords
        from modules.draft_generator import generate_draft
        from modules.qc_engine import run_qc, apply_qc_suggestions
        from modules.seo_engine import run_seo, apply_seo_suggestions
        from modules.insight_extractor import get_insights_by_ids as _gbi
        total = len(articles)
        chain = " → draft → QC → SEO → done" if auto_advance else " → keywords"
        _set_progress(task_id, f"Starting — {total} articles to process{chain}...")
        done = 0
        for a in articles:
            aid = a["id"]
            short = a["topic"][:45]
            try:
                # ── Stage 1: Keywords ──────────────────────────────────────────
                _set_progress(task_id, f"[{done+1}/{total}] Keywords: {short}...")
                manual_kws = (a.get("keywords") or {}).get("manual", [])
                result = research_keywords(
                    a["topic"], a.get("ideal_reader", ""), a.get("angle", ""),
                    manual_keywords=manual_kws or None,
                )
                if manual_kws:
                    result["manual"] = manual_kws
                current = _ga(aid)
                current["keywords"] = result
                current["stage"] = "keywords"
                _sa(current)

                if not auto_advance:
                    done += 1
                    continue

                # ── Stage 2: Draft ─────────────────────────────────────────────
                _set_progress(task_id, f"[{done+1}/{total}] Draft: {short}...")
                current = _ga(aid)
                draft = generate_draft(current)
                current["draft"] = draft
                current["stage"] = "draft"
                _sa(current)

                # ── Stage 3: QC ────────────────────────────────────────────────
                _set_progress(task_id, f"[{done+1}/{total}] QC: {short}...")
                current = _ga(aid)
                linked_ids = current.get("insight_ids_used") or []
                directive_insights = _gbi(linked_ids) if linked_ids else []
                qc_result = run_qc(
                    current.get("draft", ""),
                    current.get("topic", ""),
                    current.get("ideal_reader", ""),
                    directive_insights=directive_insights,
                )
                current["qc_result"] = qc_result
                current["stage"] = "qc"
                if auto_apply_qc:
                    critical = [s for s in qc_result.get("suggestions", []) if s.get("severity") in ("critical", "major")]
                    if critical:
                        _set_progress(task_id, f"[{done+1}/{total}] Applying {len(critical)} QC fixes: {short}...")
                        current["draft"] = apply_qc_suggestions(current["draft"], critical)
                _sa(current)

                # ── Stage 4: SEO ───────────────────────────────────────────────
                _set_progress(task_id, f"[{done+1}/{total}] SEO: {short}...")
                current = _ga(aid)
                seo_result = run_seo(
                    current.get("draft", ""),
                    current.get("keywords", {}),
                    current.get("topic", ""),
                    pillar=current.get("pillar", ""),
                )
                current["seo_result"] = seo_result
                current["stage"] = "seo"
                if auto_apply_seo:
                    critical = [s for s in seo_result.get("suggestions", []) if s.get("severity") in ("critical", "major")]
                    if critical:
                        _set_progress(task_id, f"[{done+1}/{total}] Applying {len(critical)} SEO fixes: {short}...")
                        current["draft"] = apply_seo_suggestions(current["draft"], critical)
                _sa(current)

                # ── Stage 5: Done ──────────────────────────────────────────────
                current = _ga(aid)
                current["stage"] = "done"
                _sa(current)

                done += 1
            except Exception as e:
                _set_progress(task_id, f"Error on '{short}': {e}")
        _set_progress(task_id, f"Batch complete — {done}/{total} articles processed.")
        _set_progress(task_id, "DONE")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id, "queued": len(articles), "auto_advance": auto_advance})


@app.route("/api/articles/<article_id>/relink-insights", methods=["POST"])
def api_relink_insights(article_id):
    from modules.topic_planner import get_article, save_article
    a = get_article(article_id)
    if not a:
        abort(404)
    from modules.draft_generator import _select_relevant_insights
    topic = a.get("topic", "")
    pillar = a.get("pillar", "")
    insights = _select_relevant_insights(topic, [], limit=8)
    ids = [ins["id"] for ins in insights]
    a["insight_ids_used"] = ids
    save_article(a)
    return jsonify({"ok": True, "linked": len(ids)})


@app.route("/api/articles/<article_id>/apply", methods=["POST"])
def api_apply_suggestions(article_id):
    from modules.topic_planner import get_article, save_article
    a = get_article(article_id)
    if not a:
        abort(404)

    body = request.json or {}
    suggestion_type = body.get("type", "qc")
    suggestions = body.get("suggestions", [])
    task_id = f"apply_{article_id}_{suggestion_type}"

    def _run():
        from modules.topic_planner import get_article as _ga, save_article as _sa
        try:
            current = _ga(article_id)
            _set_progress(task_id, f"Applying {len(suggestions)} {suggestion_type.upper()} suggestions...")
            if suggestion_type == "qc":
                from modules.qc_engine import apply_qc_suggestions
                current["draft"] = apply_qc_suggestions(current["draft"], suggestions)
            elif suggestion_type == "seo":
                from modules.seo_engine import apply_seo_suggestions
                current["draft"] = apply_seo_suggestions(current["draft"], suggestions)
            _sa(current)

            # Run decoupled validation if enabled
            settings = _get_settings()
            if settings.get("auto_validate", True):
                _set_progress(task_id, "Running independent validation check...")
                try:
                    from modules.validation_engine import run_validation
                    validation = run_validation(current)
                    current["validation_result"] = validation
                    _sa(current)
                except Exception as ve:
                    pass  # validation is best-effort, never block apply

            _set_progress(task_id, "DONE")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/articles/<article_id>/export")
def api_export_article(article_id):
    from modules.topic_planner import get_article
    a = get_article(article_id)
    if not a:
        abort(404)
    out_path = config.OUTPUT_DIR / f"{article_id}.md"
    draft = a.get('draft', '')
    content = draft if draft.startswith('# ') else f"# {a.get('topic', 'Article')}\n\n{draft}"
    out_path.write_text(content)
    return send_file(str(out_path), as_attachment=True, download_name=f"{a.get('topic', 'article')[:60]}.md")


# ── Demo Connector ──────────────────────────────────────────────────────────────

@app.route("/api/demo/search")
def api_demo_search():
    from modules.demo_connector import search_demos
    q = request.args.get("q", "")
    return jsonify(search_demos(q))


@app.route("/api/demo/find")
def api_demo_find():
    from modules.demo_connector import find_best_demos
    topic = request.args.get("topic", "")
    angle = request.args.get("angle", "")
    num = int(request.args.get("num", 3))
    return jsonify(find_best_demos(topic, angle, num))


@app.route("/api/demo/screenshot/<path:filepath>")
def api_demo_screenshot(filepath):
    full_path = config.DEMO_SCREENSHOTS_DIR / filepath
    if not full_path.exists():
        abort(404)
    return send_file(str(full_path))


@app.route("/api/demo/stats")
def api_demo_stats():
    from modules.demo_connector import get_demo_stats
    return jsonify(get_demo_stats())


@app.route("/api/demo/knowledge")
def api_demo_knowledge():
    from modules.demo_connector import get_knowledge_status
    return jsonify(get_knowledge_status())


@app.route("/api/demo/deep-scan", methods=["POST"])
def api_demo_deep_scan():
    body = request.json or {}
    topic = body.get("topic", "").strip()
    if not topic:
        return jsonify({"error": "topic required"}), 400
    task_id = f"deepscan_{int(time.time())}"

    def _run():
        from modules.demo_connector import deep_scan_for_topic
        try:
            _set_progress(task_id, f"Running deep demo scan for: {topic}...")
            result = deep_scan_for_topic(topic)
            if result.get("error"):
                _set_progress(task_id, f"ERROR: {result['error']}")
            else:
                count = result.get("demos_scanned", result.get("total", 0))
                _set_progress(task_id, f"DONE — scanned {count} demos")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


# ── Training Library ────────────────────────────────────────────────────────────

@app.route("/api/training/formats")
def api_get_formats():
    from modules.template_learner import get_formats
    return jsonify(get_formats())


@app.route("/api/training/add", methods=["POST"])
def api_add_training_url():
    body = request.json or {}
    url = body.get("url", "").strip()
    label = body.get("label", "")
    if not url:
        return jsonify({"error": "url required"}), 400
    task_id = f"train_{int(time.time())}"

    def _run():
        from modules.template_learner import learn_from_url
        try:
            _set_progress(task_id, f"Fetching and analysing {url}...")
            result = learn_from_url(url, label, progress_cb=lambda m: _set_progress(task_id, m))
            _set_progress(task_id, "DONE" if result and not result.get("error") else f"ERROR: {result}")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/training/add-text", methods=["POST"])
def api_add_training_text():
    body = request.json or {}
    text = body.get("text", "").strip()
    label = body.get("label", "")
    angle_hint = body.get("angle_hint", "")
    if not text:
        return jsonify({"error": "text required"}), 400
    task_id = f"train_text_{int(time.time())}"

    def _run():
        from modules.template_learner import learn_from_text
        try:
            _set_progress(task_id, "Extracting framework structure with Claude...")
            result = learn_from_text(text, label, angle_hint, progress_cb=lambda m: _set_progress(task_id, m))
            _set_progress(task_id, "DONE" if result and not result.get("error") else f"ERROR: {result}")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/training/formats/delete", methods=["POST"])
def api_delete_format():
    body = request.json or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    from modules.template_learner import delete_format
    deleted = delete_format(url)
    return jsonify({"ok": deleted})


# ── Internal Link Library ──────────────────────────────────────────────────────

@app.route("/api/links")
def api_get_links():
    from modules.link_library import get_all_links
    return jsonify(get_all_links())


@app.route("/api/links/add", methods=["POST"])
def api_add_link():
    body = request.json or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    task_id = f"link_{abs(hash(url)) % 100000}"

    def _run():
        from modules.link_library import index_url
        try:
            result = index_url(url, progress_cb=lambda m: _set_progress(task_id, m))
            if result:
                _set_progress(task_id, "DONE")
            else:
                _set_progress(task_id, "ERROR: Could not index URL")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/links/delete", methods=["POST"])
def api_delete_link():
    body = request.json or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    from modules.link_library import remove_link
    return jsonify({"ok": remove_link(url)})


# ── Models ─────────────────────────────────────────────────────────────────────

@app.route("/api/models/status")
def api_models_status():
    from modules.model_manager import get_status
    return jsonify(get_status())


@app.route("/api/models/detect", methods=["POST"])
def api_models_detect():
    if not config.ANTHROPIC_API_KEY:
        return jsonify({"error": "Anthropic API key not set"}), 400
    task_id = f"model_detect_{int(time.time())}"

    def _run():
        from modules.model_manager import detect_models
        try:
            _set_progress(task_id, "Detecting available models...")
            detected = detect_models()
            _set_progress(task_id, f"DONE — sonnet: {detected.get('sonnet','?')}, haiku: {detected.get('haiku','?')}")
        except Exception as e:
            _set_progress(task_id, f"ERROR: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


# ── Scheduler ───────────────────────────────────────────────────────────────────

@app.route("/api/scheduler/trigger", methods=["POST"])
def api_trigger_scheduler():
    body = request.json or {}
    job = body.get("job", "insight_scan")
    ok = sched.trigger(job)
    return jsonify({"ok": ok})


@app.route("/api/scheduler/log")
def api_scheduler_log():
    return jsonify(sched.get_log())


# ── Settings ────────────────────────────────────────────────────────────────────

@app.route("/api/settings")
def api_get_settings():
    s = _get_settings()
    s["api_keys_set"] = {
        "anthropic": bool(config.ANTHROPIC_API_KEY),
        "ahrefs": bool(config.AHREFS_API_TOKEN),
        "grain": bool(config.GRAIN_API_TOKEN),
        "sybill": bool(config.SYBILL_API_TOKEN),
    }
    return jsonify(s)


@app.route("/api/settings", methods=["PUT"])
def api_update_settings():
    body = request.json or {}
    current = _get_settings()
    # Keep api_keys separate — only update via /api/settings/keys
    body.pop("api_keys", None)
    current.update(body)
    _save_settings(current)
    sched.reschedule()
    return jsonify(current)


@app.route("/api/settings/keys", methods=["PUT"])
def api_save_keys():
    body = request.json or {}
    current = _get_settings()
    existing_keys = current.get("api_keys", {})
    # Only overwrite non-empty values so partial saves don't blank existing keys
    for k, v in body.items():
        if v and v.strip():
            existing_keys[k] = v.strip()
    current["api_keys"] = existing_keys
    _save_settings(current)
    config.reload_keys()
    return jsonify({"ok": True})


@app.route("/api/settings/key-health")
def api_key_health():
    """Live-ping each configured API key and return ok / invalid / not_set."""
    import requests as req_lib

    results = {}

    # ── Anthropic ──────────────────────────────────────────────────────────────
    if not config.ANTHROPIC_API_KEY:
        results["anthropic"] = "not_set"
    else:
        try:
            import anthropic as ant
            c = ant.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            c.messages.create(model="claude-haiku-4-5-20251001", max_tokens=5, messages=[{"role":"user","content":"hi"}])
            results["anthropic"] = "ok"
        except ant.AuthenticationError:
            results["anthropic"] = "invalid"
        except Exception:
            results["anthropic"] = "ok"   # other errors (rate limit etc) mean key is valid

    # ── Ahrefs ────────────────────────────────────────────────────────────────
    if not config.AHREFS_API_TOKEN:
        results["ahrefs"] = "not_set"
    else:
        try:
            r = req_lib.get(
                "https://api.ahrefs.com/v3/subscription-info/limits-and-usage",
                headers={"Authorization": f"Bearer {config.AHREFS_API_TOKEN}"},
                timeout=8
            )
            results["ahrefs"] = "ok" if r.status_code == 200 else "invalid"
        except Exception:
            results["ahrefs"] = "error"

    # ── Grain ─────────────────────────────────────────────────────────────────
    if not config.GRAIN_API_TOKEN:
        results["grain"] = "not_set"
    else:
        try:
            r = req_lib.get(
                "https://api.grain.com/v2/recordings?limit=1",
                headers={"Authorization": f"Bearer {config.GRAIN_API_TOKEN}"},
                timeout=8
            )
            results["grain"] = "ok" if r.status_code in (200, 204) else "invalid"
        except Exception:
            results["grain"] = "error"

    # ── Sybill ────────────────────────────────────────────────────────────────
    # Sybill is push-only (webhook) — no GET endpoint to ping; just report set/not_set
    results["sybill"] = "set" if config.SYBILL_API_TOKEN else "not_set"

    return jsonify(results)


# ── Export / Share ──────────────────────────────────────────────────────────────

BUNDLE_FOLDER = "Storylane Customer Insights Content Engine - Complete"
ENGINE_FOLDER  = "Storylane Customer Insights Content Engine"

MAC_SETUP_SCRIPT = """\
#!/bin/bash
# This script unlocks all the files so you can double-click the launchers normally.
# If double-clicking this file gives you a security warning, right-click it → Open → Open.

cd "$(dirname "$0")"

echo ""
echo "========================================"
echo "  Storylane Content Engine — Mac Setup"
echo "========================================"
echo ""
echo "Step 1/2: Removing macOS security flags on all files..."
xattr -rd com.apple.quarantine . 2>/dev/null
echo "         Done."
echo ""
echo "Step 2/2: Making launcher files executable..."
chmod +x "Storylane Customer Insights Content Engine/Content Engine.command" 2>/dev/null
chmod +x "storylane-demo-classifier/Start Classifier.command" 2>/dev/null
echo "         Done."
echo ""
echo "========================================"
echo "  Setup complete! Here's what to do now:"
echo "========================================"
echo ""
echo "  1. Double-click:  storylane-demo-classifier/Start Classifier.command"
echo "     Wait until a browser tab opens at localhost:8000."
echo "     Keep that Terminal window open (just minimise it)."
echo ""
echo "  2. Double-click:  Storylane Customer Insights Content Engine/Content Engine.command"
echo "     Wait until a browser tab opens at localhost:8001."
echo "     Keep that Terminal window open too."
echo ""
echo "  3. In the browser at localhost:8001 → Settings → API Keys → paste your key."
echo ""
echo "  Next time: just double-click both launchers. No setup needed again."
echo ""
echo "Press any key to close this window..."
read -n 1
"""

SETUP_README = """\
STORYLANE CONTENT ENGINE — HOW TO OPEN
=======================================

READ THIS FIRST — macOS blocks downloaded files by default.
You must do the two steps below before anything will work.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIRST TIME ONLY  (takes about 2 minutes)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — Unlock the folder in Terminal
──────────────────────────────────────
  This tells macOS "I trust everything in this folder."

  a) Open Terminal
     (Press Cmd+Space, type "Terminal", press Enter)

  b) In Terminal, type the following — then add a SPACE at the end
     (do NOT press Enter yet):

        xattr -rd com.apple.quarantine

  c) Now open Finder and find the folder named:
        "Storylane Customer Insights Content Engine - Complete"
     Drag that folder into the Terminal window and drop it.
     The full path will appear next to your command automatically.

  d) Now press Enter.

  Nothing will appear to happen — that is correct. You just unlocked every file.


STEP 2 — Run the setup script (handles everything else)
────────────────────────────────────────────────────────
  Double-click the file:  ▶ Start Here — Mac Setup.command

  A black Terminal window opens and runs automatically.
  It will tell you when it's done and what to do next.

  (If macOS still shows a security warning: right-click the file → Open → Open)


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STARTING THE TOOLS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Always start in this order:

  1. Double-click:  storylane-demo-classifier/Start Classifier.command
     A black window opens and installs things the first time (~1 min).
     Your browser opens to http://localhost:8000 when it's ready.
     ⚠️  Keep this window open (minimise it — do NOT close it).

  2. Double-click:  Storylane Customer Insights Content Engine/Content Engine.command
     Same process. Your browser opens to http://localhost:8001.
     ⚠️  Keep this window open too.

  3. In the Content Engine (localhost:8001):
     Go to Settings → API Keys → paste your Anthropic API key → Save.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EVERY TIME AFTER THAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Just double-click both launchers. No Terminal needed.
  Classifier first → Engine second. That's it.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TROUBLESHOOTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  "Operation not permitted"
      → You skipped Step 1. Run the xattr command again.

  "Permission denied" or nothing happens on double-click
      → You skipped Step 2. Double-click the setup script.

  Browser shows "This site can't be reached"
      → The launcher window was closed. Re-open it and wait for it to start.

  Port already in use
      → Open Activity Monitor (Cmd+Space → Activity Monitor),
        search for "python", and quit any running Python processes.
        Then try launching again.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Questions? Contact Prashil.
"""


def _add_dir_to_zip(zf: zipfile.ZipFile, source_dir: Path, zip_prefix: str,
                    exclude_dirs: set, settings_arcname: str = None):
    """Add all files from source_dir into zip under zip_prefix/."""
    for item in source_dir.rglob("*"):
        if not item.is_file():
            continue
        parts = item.relative_to(source_dir).parts
        if parts[0] in exclude_dirs:
            continue
        if item.name.startswith(".") and item.name != ".gitignore":
            continue

        rel = item.relative_to(source_dir)
        arcname = f"{zip_prefix}/{rel}" if zip_prefix else str(rel)

        if settings_arcname and str(rel) == settings_arcname:
            try:
                d = json.loads(item.read_text())
                d["api_keys"] = {k: "" for k in d.get("api_keys", {})}
                zf.writestr(arcname, json.dumps(d, indent=2))
            except Exception:
                zf.write(item, arcname)
        else:
            zf.write(item, arcname)


@app.route("/api/export-zip")
def api_export_zip():
    """
    Stream a portable zip of the content engine (and optionally the demo
    classifier) with API keys blanked and a Mac quarantine-fix script included.

    Query params:
      bundle=1  — also include the sibling storylane-demo-classifier/ folder
    """
    EXCLUDE_DIRS = {"venv", "__pycache__", "output", ".git", ".claude"}
    bundle = request.args.get("bundle") == "1"
    classifier_dir = config.DEMO_CLASSIFIER_DIR

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if bundle:
            # Both tools go into a parent folder so sibling structure is preserved
            _add_dir_to_zip(zf, config.BASE_DIR,
                            f"{BUNDLE_FOLDER}/{ENGINE_FOLDER}",
                            EXCLUDE_DIRS, "data/settings.json")
            if classifier_dir.exists():
                _add_dir_to_zip(zf, classifier_dir,
                                f"{BUNDLE_FOLDER}/storylane-demo-classifier",
                                EXCLUDE_DIRS | {"screenshots"})
                # Empty screenshots placeholder so the path exists
                zf.writestr(f"{BUNDLE_FOLDER}/storylane-demo-classifier/screenshots/.keep", "")
            # Mac setup script + readme at root of the bundle folder
            zf.writestr(f"{BUNDLE_FOLDER}/▶ Start Here — Mac Setup.command", MAC_SETUP_SCRIPT)
            zf.writestr(f"{BUNDLE_FOLDER}/README.txt", SETUP_README)
            download_name = f"{BUNDLE_FOLDER}.zip"
        else:
            # Engine only — flat structure, no wrapper folder
            _add_dir_to_zip(zf, config.BASE_DIR, "",
                            EXCLUDE_DIRS, "data/settings.json")
            download_name = f"{ENGINE_FOLDER} - Engine Only.zip"

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=download_name,
    )


if __name__ == "__main__":
    sched.start()
    app.run(host=config.HOST, port=config.PORT, debug=False)
