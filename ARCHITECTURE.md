# Storylane Customer Insights Content Engine

> **Start:** Double-click `Content Engine.command` inside `storylane-content-engine/` — auto-creates venv and installs deps on first run, then opens the browser at `http://localhost:8001`
> **Port:** 8001 · Demo classifier runs on 8000 — do not conflict
> **Manual start:** `cd storylane-content-engine && source venv/bin/activate && python3 app.py`

---

## Working on This Codebase

At the end of every session, ask:
> *"Shall I update ARCHITECTURE.md to reflect the changes we made?"*

Keep this document the single source of truth — no stale sections, no roadmap items that are already built. It should be readable by a non-technical stakeholder (top section) and a developer picking up cold (technical sections). Update whenever a module is added, a route changes, a setting is added, or a planned item gets built.

---

## For Non-Technical Readers

**What is this tool?**

The Content Engine is an internal tool that turns Storylane customer call recordings into publication-ready SEO articles. Instead of starting from a blank page, it pulls intelligence from sales calls and customer conversations, generates article ideas, researches keywords, writes drafts, and checks its own work — all with Storylane's voice and product presence baked in.

**How it works, step by step:**

1. **It learns about Storylane first.** Point it at the website (homepage, pricing, docs, feature pages) and it distills everything into a compact intelligence brief. Every article it writes draws on this brief — so it always writes from Storylane's perspective, not generically.

2. **It listens to what customers say.** Sales calls recorded in Grain or Sybill get pulled in automatically. It extracts real pain points, objections, exact quotes, and metrics from customers — the raw material that makes an article feel authoritative and specific.

3. **It proposes article ideas.** Based on customer insights and content pillar gaps, it generates topic proposals — each with a specific angle, the ideal reader described precisely, and a reason someone would actually read it. Every topic records which specific customer insights informed it.

4. **It researches keywords.** Runs a 3-pass process: SERP analysis first, then keyword brainstorming that works backwards from parent categories (not just the niche topic title), then Ahrefs validation for volume and difficulty. You can also paste your own keywords manually — they're always included regardless of research scores.

5. **It writes the draft.** Uses the company intelligence, directive customer insights (the ones that inspired the topic), and real product demo examples. Two things are non-negotiable in every draft: Storylane must appear as a genuine substantive protagonist throughout (not a footnote), and every article ends with a closing CTA section linking to Storylane's free trial.

6. **It QC checks its own work.** Scores 7 dimensions including insight authenticity — did the draft actually use the customer language from source calls? Gives specific suggestions to fix what's weak.

7. **It runs SEO checks.** Keyword density, heading structure, missing subtopics, internal links, meta title/description — all checked and flagged.

8. **You review and export.** Accept or reject suggestions, edit the draft directly in the browser. When you export, it first checks that the source customer quotes and metrics are still present in the final article — if any have been paraphrased away or dropped, it flags them before you download.

**Other things you can do:**

- **Manual keyword dump** — paste keywords you found manually in the keyword panel; they're injected into every draft regardless of Ahrefs scores
- **Process All Ideas** — one button on the Pipeline page kicks off keyword research on every idea-stage article at once
- **Active / Processed insights** — Insights tab splits into Active (not yet used in any article) and Processed (already moved to pipeline)
- **Topic lineage** — Info tab on any article shows exactly which customer calls and insights informed that topic
- **Retroactive insight linking** — if an article has no source insights linked, one click scores and links the most relevant ones
- **Go back a stage** — step any article backward and re-run checks fresh
- **Training Library** — add article URLs to teach the system different writing formats per angle
- **Auto-advance** — chain all pipeline stages automatically, with optional auto-apply of suggestions
- **Validation tab** — after applying suggestions, a fresh independent Claude pass checks insight fidelity, meaning preservation, and keyword coherence — without bias from the previous QC/SEO runs
- **Export & Share** — Settings has a one-click export that bundles both this tool and the Demo Classifier into a portable zip (API keys stripped)

---

## For Technical Readers

### Architecture Overview

Single-machine Flask app (port 8001) with a SQLite insight store, JSON file state, APScheduler background jobs, and Anthropic Claude API for all generation. No database beyond SQLite for insights — articles, pillars, settings, and keyword cache are JSON files in `data/`. Local-first design; all paths are relative.

**Stack:** Python 3.13 · Flask · APScheduler · Anthropic SDK · SQLite · vanilla JS single-file frontend

**Sibling tool:** `storylane-demo-classifier/` at `BASE_DIR.parent / "storylane-demo-classifier"` — runs on port 8000. Content engine calls `/query-engine` on it; falls back to local index scoring if classifier is offline or has no API key.

---

### File Structure

```
storylane-content-engine/
├── app.py                        # Flask server + all routes (port 8001)
├── scheduler.py                  # APScheduler: insight scan every 6h, topic gen every 24h
├── config.py                     # All paths, API keys, reload_keys(), load_settings(), DEFAULT_SETTINGS
├── modules/
│   ├── model_manager.py          # create_message(tier) — Sonnet/Haiku wrapper with fallback chain
│   ├── company_brain.py          # URL scraping + file upload (PDF/PPTX/XLSX/CSV) → company_brief.json
│   ├── insight_extractor.py      # Transcripts/dumps → structured insights → insights.db (SQLite)
│   ├── grain_connector.py        # Grain REST API v2 poller — cursor-paginated, keyword+length filtered
│   ├── sybill_connector.py       # Sybill REST API poller — mirrors grain_connector.py pattern
│   ├── pillar_map.py             # Content pillar coverage map — tracks published/in-pipeline/missing
│   ├── topic_planner.py          # Insights + pillar gaps → topic proposals with insight lineage
│   ├── keyword_researcher.py     # SERP pass → parent-category brainstorm → Ahrefs validate + manual keywords
│   ├── demo_connector.py         # POST /query-engine on classifier → local index scoring fallback
│   ├── draft_generator.py        # Directive insight injection + Storylane enforcement + CTA rule
│   ├── qc_engine.py              # 7-dimension scoring (incl. insight_authenticity) + suggestions + apply
│   ├── seo_engine.py             # Structural checks + Claude semantic pass + smart internal link check
│   ├── template_learner.py       # Article URL → structural template + mindset layer → format context
│   ├── link_library.py           # Internal link index: scrape → keyword/anchor extract → inject into drafts
│   └── validation_engine.py      # Decoupled post-apply validation: insight fidelity + meaning + keywords
├── data/
│   ├── company_brief.json        # Distilled Storylane intelligence (~800–1200 tokens)
│   ├── insights.db               # SQLite: all extracted insights
│   ├── sources.json              # Processed source IDs — prevents re-processing
│   ├── articles.json             # All pipeline articles: { "articles": [...] }
│   ├── content_pillars.json      # Pillar coverage map
│   ├── content_formats.json      # Training library templates
│   ├── link_index.json           # Internal link library index
│   ├── kw_cache.json             # Ahrefs keyword cache (no TTL — manual invalidation)
│   ├── demo_query_cache.json     # Demo classifier query cache (1h TTL)
│   └── settings.json             # All config: API keys, scheduler, automation, rubrics, filters
├── watch/
│   ├── grain/                    # Drop Grain .txt/.vtt/.md exports here for manual ingestion
│   └── sybill/                   # Drop Sybill exports here for manual ingestion
├── output/articles/              # Exported .md files
├── Content Engine.command        # Portable launcher — auto-venv, auto-install, opens browser
├── SETUP.md                      # First-time setup guide for new recipients
└── static/index.html             # Full dashboard UI — all tabs, all JS, no build step
```

---

### API Routes

| Method | Route | What it does |
|---|---|---|
| GET | `/api/status` | Health + article counts by stage + scheduler log |
| GET | `/api/company-brief` | Returns distilled company_brief.json |
| POST | `/api/company-brain/scan` | Scrapes URLs + processes docs → company_brief.json |
| POST | `/api/company-brain/upload` | Extracts text from uploaded file (PDF/PPTX/etc.) |
| GET | `/api/insights` | Insights with optional filters (source_type, tag, min_confidence, limit) |
| POST | `/api/insights/by_ids` | Fetch specific insights by UUID array |
| POST | `/api/insights/dump` | Text dump → extract_insights_from_text() |
| POST | `/api/insights/upload` | Uploaded transcript → extract_insights_from_text() |
| POST | `/api/scheduler/trigger` | `{ "job": "sybill_poll" }` manually kicks off Sybill poll |
| POST | `/api/watch/process` | Scans watch/grain/ and watch/sybill/ for new files |
| GET | `/api/watch/files` | Files currently in watch folders |
| GET | `/api/pillars` | Returns `{ map, gaps }` |
| POST | `/api/topics/generate` | Generates N topic proposals → articles at "idea" stage |
| GET | `/api/articles` | All articles (optional `?stage=` filter) |
| POST | `/api/articles` | Create article manually |
| GET | `/api/articles/:id` | Single article |
| PUT | `/api/articles/:id` | Update any article fields (stage rollback, etc.) |
| PUT | `/api/articles/:id/draft` | Save manual draft edits |
| POST | `/api/articles/:id/keywords` | Run keyword research → "keywords" stage |
| PUT | `/api/articles/:id/keywords/manual` | Save manually entered keywords to `keywords.manual` |
| POST | `/api/articles/:id/draft` | Generate draft → "draft" stage |
| POST | `/api/articles/:id/qc` | Run QC pass → "qc" stage |
| POST | `/api/articles/:id/seo` | Run SEO pass → "seo" stage |
| POST | `/api/articles/:id/apply` | Apply accepted suggestions; runs decoupled validation if auto_validate on |
| POST | `/api/articles/:id/insight-check` | Pre-export check: are source quotes/metrics still in the draft? |
| POST | `/api/articles/:id/relink-insights` | Score + link most relevant insights to an article retroactively |
| GET | `/api/articles/:id/export` | Download article as .md (triggered via exportWithInsightCheck in UI) |
| POST | `/api/pipeline/process-all` | Batch keyword research on all articles at a given stage |
| GET | `/api/demo/search` | Search demo index by query |
| GET | `/api/demo/find` | Find best-fit demos for a topic/angle |
| POST | `/api/demo/deep-scan` | Trigger deep scan on classifier |
| GET | `/api/demo/knowledge` | Classifier knowledge status |
| POST | `/api/training/add` | Add article URL to training library (async) |
| GET | `/api/training/formats` | List learned formats |
| POST | `/api/training/formats/delete` | Delete a learned format by URL |
| GET | `/api/links` | All indexed internal links |
| POST | `/api/links/add` | Scrape and index a URL (async) |
| POST | `/api/links/delete` | Remove a URL from the link index |
| GET | `/api/progress/:task_id` | Poll async task progress |
| GET | `/api/settings` | Full settings including api_keys_set booleans |
| PUT | `/api/settings` | Save scheduler/automation/rubric/filter settings |
| PUT | `/api/settings/keys` | Save API keys (partial — only overwrites non-empty values) |
| GET | `/api/export-zip` | Stream portable zip — `?bundle=1` includes demo classifier |

All Claude-calling operations are async: return `{ task_id }` immediately, frontend polls `GET /api/progress/:task_id` every 1.2s until `DONE` or `ERROR:...`.

---

### Article Data Schema

Each article in `articles.json` has:

```json
{
  "id": "uuid",
  "topic": "...",
  "ideal_reader": "...",
  "why_read": "...",
  "angle": "thought_leadership | listicle | opinion | data_led | how_to | comparison | digital_pr",
  "pillar": "...",
  "strategic_intent": ["eeat_signal", "backlinkable", "digital_pr", "primary_voice", "pillar_anchor"],
  "gap_it_fills": "...",
  "stage": "idea | keywords | draft | qc | seo | done",
  "keywords": {
    "primary": { "keyword": "...", "volume": 0, "difficulty": 0, "source": "ahrefs|brainstorm|manual" },
    "secondary": [ /* same shape */ ],
    "manual": ["keyword one", "keyword two"],
    "serp": { "dominant_format": "...", "avg_word_count": 0, "featured_snippet": false },
    "settings_used": { "country": "us", "max_kd": 70, "min_volume": 0 }
  },
  "draft": "markdown string",
  "qc_result": { "scores": {}, "suggestions": [] },
  "seo_result": { "suggestions": [] },
  "validation_result": { "insight_fidelity": {}, "meaning_preservation": {}, "keyword_coherence": {}, "overall_verdict": "pass|warn|fail" },
  "social_signals": [ { "format": "carousel|post|short_video|poll", "hook": "...", "upgrade_to_article": false } ],
  "insight_ids_used": ["uuid", "..."],
  "overlap_warning": { "similar_to": "...", "reason": "..." },
  "created_at": "ISO8601",
  "updated_at": "ISO8601"
}
```

---

### Frontend Architecture

Single file: `static/index.html` — no build step, no framework. Vanilla JS + `marked.min.js` (CDN) for Markdown rendering.

**Global state `S`:** `currentTab, currentArticle, sidebarTab, insightTab, qcSuggestions, seoSuggestions, taskPollers, settings, editMode`

**Tab structure:**
- `tab-dashboard` — pipeline flow visualization + scheduler log
- `tab-insights` — Active / Processed tabs; insight cards with raw input preview and Grain deep-link
- `tab-pipeline` — Kanban: Ideas → Keywords → Draft → QC → SEO → Done; auto-advance toggle; "Process All Ideas" batch button
- `tab-editor` — Article Editor: stage bar, rendered draft, QC / SEO / Validation / Info sidebar tabs
- `tab-training` — Training Library: multi-URL queue, sequential processing, per-card delete
- `tab-settings` — Company scan, scheduler, automation toggles, rubrics, keyword params, call filters, internal link library, API keys, models, Export & Share

**Key JS functions:**
- `loadPipeline()` — renders Kanban from articles API; syncs auto-advance toggle
- `processAllIdeas()` — confirms count, calls `/pipeline/process-all`, polls progress
- `openArticle(id)` — loads article, resets editMode, switches to editor tab
- `renderEditor()` — re-renders entire editor view from `S.currentArticle`
- `renderTopbarActions(a)` — stage-appropriate action buttons + Edit toggle + go-back button
- `goBackStage(targetStage)` — moves article backward via `PUT /articles/:id`
- `pollTask(taskId, label, onDone)` — shared polling loop for all async jobs
- `_autoAdvance()` — checks `S.settings.auto_advance`, chains next stage
- `renderKeywordPanel(a)` — SERP intel panel, keyword table (with volume/KD/source badges), manual keyword textarea + save button
- `saveManualKeywords(articleId)` — PUTs to `/keywords/manual`, refreshes editor
- `exportWithInsightCheck()` — calls `/insight-check` first; if issues found, shows modal with flagged elements and "Export anyway" option; otherwise exports directly
- `exportArticle()` — direct download via `/articles/:id/export`
- `relinkInsights(articleId)` — calls `/relink-insights`, refreshes Info tab
- `loadInsights()` — fetches insights filtered by `S.insightTab` (active = unused, processed = used)
- `setInsightTab(tab)` — switches between Active / Processed insight views
- `renderInsightCard(ins)` — source name, pain points, quotes, metrics, tags; raw_input toggle; Grain deep-link
- `renderSidebarTab(tab)` — renders QC scores / SEO suggestions / Validation results / Info (lineage + overlap warning)
- `saveSettings() / loadSettings()` — full settings roundtrip
- `saveApiKeys()` — partial key save (non-empty values only)
- `exportToolZip(bundle)` — downloads zip; `bundle=true` includes demo classifier + Mac setup script

---

### Module Details

**`model_manager.py`**
`create_message(tier, **kwargs)` — `"sonnet"` or `"haiku"`. Resolves model ID from `settings.json`, falls back to config defaults. All generation modules use this exclusively.

**`insight_extractor.py`**
SQLite schema per insight: `id, source_id, source_type, source_name, author, customer_segment, pain_points, quotes, objections, use_cases, metrics, competitors, tags, confidence, extracted_at, used_in, raw_summary, raw_input`. Source IDs in `sources.json` prevent reprocessing. `used_in` (JSON array of article IDs) is updated when an insight is used at topic generation or draft time. `raw_input` stores the first 8,000 chars of the original text. `ALTER TABLE` migration runs on init. `get_insights_by_ids(ids)` fetches specific insights by UUID list. `_compute_saturation()` attaches a `saturation_score` (0–1) to each insight based on tag overlap with peers.

**`grain_connector.py`**
Grain REST API v2 poller. Filters: external participants required; excludes scrum/1:1/all-hands patterns; keyword inclusion filter; `min_duration_minutes` and `min_transcript_words` thresholds. Fetches full transcript via `/recordings/:id/transcript?format=txt`, falls back to `ai_summary`. Saves cursor + processed IDs to `sources.json["grain"]`. Runs after watch-folder scan on every scheduler cycle.

**`sybill_connector.py`**
REST API poller — identical pattern to `grain_connector.py`. Calls `GET /v1/conversations?meeting_type=EXTERNAL` with Bearer `sk_live_...` auth. Paginates through all external conversations since `last_run` (default: 90-day lookback on first run). For each new conversation, fetches `GET /v1/conversations/{id}` for full transcript + AI summary. Filters: EXTERNAL type only, title exclusion patterns (scrum/standup/1:1/all-hands/interview), `min_duration_minutes`, `min_transcript_words`, keyword presence in transcript. Builds text block from `transcript[].{speaker, text}` entries; appends `summary.{Outcome, Key Takeaways, Pain Points}` as extra signal. Saves `{ last_run, processed_ids }` to `sources.json["sybill"]`. No ngrok or webhook required — pure pull integration. Rate limit: adds 0.3s delay between list pages, 0.5s between transcript fetches.

**`topic_planner.py`**
Fetches top 20 insights (min confidence 0.4), tags each `INS_00`…`INS_19` in the prompt. Claude returns which short keys it used; resolved to real UUIDs → stored in `article.insight_ids_used`. Marks contributing insights as used (`mark_insight_used`) at generation time. Post-generation Haiku pass checks new proposals against existing pipeline topics for semantic overlap → stores `overlap_warning` on overlapping articles. Insights with `saturation_score ≥ 0.3` get a `[SATURATED]` note in the prompt to deprioritise them.

**`keyword_researcher.py`**
3-pass pipeline: (1) **SERP pass** — Ahrefs `/serp-overview` for dominant format, avg word count, featured snippet. (2) **Brainstorm** — Claude Haiku generates 15 candidates working backwards from parent categories and broader problem spaces, not just the niche topic title. Manual keywords are seeded in and guaranteed in output. (3) **Ahrefs validation** — `/keywords-explorer/overview` for volume + KD. Manual keywords get `score += 1000` so they always top the list and are never filtered out by KD/volume thresholds. Accepts `manual_keywords: list` param; keyword research runs preserve `keywords.manual` from the article.

**`draft_generator.py`**
Loads directive insights (those in `article.insight_ids_used`) first, fills remaining slots (up to 8) with semantically relevant insights. Directive insights marked ★ with explicit instruction to use their specific language verbatim. Manual keywords from `keywords.manual` are included in the keyword instruction block. Two non-negotiable prompt rules: (7) Storylane must appear as a genuine substantive protagonist throughout — not a footnote; (8) every draft ends with a mandatory H2 closing CTA section linking to Storylane's free trial. After generation, calls `mark_insight_used()` for all referenced insights.

**`qc_engine.py`**
Scores 7 dimensions: overall, hook, fluff, relevancy, reader_value, eeat, `insight_authenticity`. Directive insight quotes/pain points are passed into the QC prompt for presence checking. Suggestion type `"insight_gap"` flags missing customer language. `apply_qc_suggestions()` applies only accepted suggestions in a second Claude pass.

**`seo_engine.py`**
Two-pass: (1) structural Python checks (keyword density, H1/H2 count, length, internal links, secondary keyword presence — handles both string and object formats); (2) Claude semantic pass for gaps, missing subtopics, meta title/description. Smart internal link check: queries `link_library` for relevant URLs, names specific missing ones in suggestions.

**`template_learner.py`**
Scrapes article URL → two-layer format extraction via Sonnet: structural template + writing mindset. Stored in `content_formats.json`. `get_format_context_for_angle(angle)` returns the best matching format as a reference block injected into draft prompts.

**`link_library.py`**
URL index in `link_index.json`. `index_url(url)` scrapes + extracts title/summary/keywords/anchor suggestions via Haiku; infers page type (blog/docs/feature/pricing/comparison). `get_relevant_links(topic, pillar, num)` scores by keyword overlap, pillar match, page type. `format_links_for_prompt(links)` produces the INTERNAL LINKS block for draft prompts.

**`validation_engine.py`**
Runs after suggestions are applied. Zero context of previous QC/SEO results — completely fresh Sonnet call. Checks: (1) insight_fidelity — source customer language preserved? (2) meaning_preservation — core argument intact? (3) keyword_coherence — selected keywords still fit the article? Each dimension returns `score/status/finding/suggestion`. Result stored as `article.validation_result`, shown in Validation sidebar tab.

---

### Settings Schema (`data/settings.json`)

```json
{
  "scheduler": {
    "insight_scan_hours": 6,
    "topic_gen_hours": 24,
    "enabled": true,
    "auto_topics_after_scan": false
  },
  "auto_apply": { "qc": false, "seo": false },
  "auto_advance": false,
  "auto_validate": true,
  "topic_gen_count": 5,
  "qc_rubric": "",
  "seo_rubric": "",
  "keyword_settings": {
    "country": "us",
    "max_kd": 70,
    "min_volume": 0,
    "skip_ahrefs": false
  },
  "grain_filter": {
    "require_keywords": ["demo", "onboarding", "pricing", "objection", "competitor", "pain point", "use case", "buyer", "trial", "evaluation"],
    "min_duration_minutes": 10,
    "min_transcript_words": 200
  },
  "sybill_filter": {
    "require_keywords": ["demo", "onboarding", "pricing", "objection", "competitor", "pain point", "use case", "buyer", "trial", "evaluation"],
    "min_duration_minutes": 10,
    "min_transcript_words": 200
  },
  "models": {
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001"
  },
  "api_keys": {
    "anthropic": "...",
    "ahrefs": "...",
    "grain": "...",
    "sybill": "..."
  }
}
```

`config.reload_keys()` must be called after saving keys — refreshes globals and clears cached `_client` handles.

---

### Automation Chain

**Auto-advance** (`auto_advance: true`): after each `pollTask` `onDone`, `_autoAdvance()` triggers the next stage. Chain: `keywords → draft → qc → seo`. At QC: if `auto_apply.qc` true, applies all critical/major suggestions before advancing. At SEO: same with `auto_apply.seo`, then calls `markDone()`. Halts at any stage where the relevant auto_apply toggle is off.

**Auto-topics after scan** (`auto_topics_after_scan: true`): after each insight scan cycle, if new insights were found, `generate_topics()` runs automatically.

**Auto-validate** (`auto_validate: true`, default): after every `apply` call, `run_validation()` fires with no prior QC/SEO context. Result stored on article, shown in Validation tab.

**Process All Ideas**: `POST /api/pipeline/process-all` respects `auto_advance`. If OFF: runs keyword research only, articles land at keywords stage. If ON: chains the full pipeline (keywords → draft → QC → SEO → done) for every idea-stage article sequentially, applying QC/SEO suggestions automatically if `auto_apply.qc`/`auto_apply.seo` are enabled. Confirm dialog shows estimated Ahrefs credits and Claude cost before starting. Progress messages are `[N/total] Stage: topic...` throughout, emitting plain `"DONE"` as the final signal.

**Scheduler jobs** (both log to Dashboard):
- `insight_scan` — watch folder + Grain poll → optional auto-topic generation
- `topic_gen` — standalone topic generation run
- Manual trigger: `POST /api/scheduler/trigger { "job": "insight_scan|topic_gen|grain_poll|sybill_poll" }`

---

### Integration Status

| Service | Status | Notes |
|---|---|---|
| Claude API | ✅ | All modules via `model_manager.create_message()` |
| Ahrefs v3 REST | ✅ | Keywords Explorer + SERP overview; results cached in `kw_cache.json` |
| Demo Classifier | ✅ | `/query-engine` with local index fallback; classifier API key optional |
| Grain | ✅ | REST API v2 poller + watch folder; wired to scheduler. 112+ calls processed. |
| Sybill | ✅ | REST API poller. `sk_live_` key works. Found 650+ historical calls on first run. Wired to scheduler + manual poll button. |
| Webflow CMS | ⏳ Deferred | Push final article via REST API v2; needs collection ID + field mapping |

---

### Sybill — Integration Notes

Sybill uses a **pure REST pull** approach — no webhook, no ngrok, no public URL required.

**Authentication:** Bearer `sk_live_...` master key. Confirmed working — returns `{"status":"ok","org_id":"...","scopes":["read"]}` from `GET /v1/health`.

**First run:** defaults to 90-day lookback. On first successful poll, found 650+ external conversations. Subsequent runs pick up only new conversations since `last_run`.

**Rate limits:** Sybill allows ~50 req/min. Connector adds delays (0.3s between list pages, 0.5s between transcript fetches) to stay within limits.

**Manual poll:** Settings → Scheduler section → "▶ Poll Sybill Now" button. Also triggered automatically on every scheduler insight scan cycle.

---

### What's Still Left to Build

| Item | Notes |
|---|---|
| **Structural brief UX** | Training Library: two paste modes — "article text" (analyze) vs "direct instructions" (store raw). Currently only analyze mode exists. |
| **Webflow publish** | Push done articles to Webflow CMS via REST API v2. Needs collection ID + field mapping per pillar. |
| **Hosted deployment** | Railway. See Hosting section. Unblocks Sybill permanently and adds team access. |

---

### Company Intelligence — Healthy Scale

The brief is a distillation, not a dump. Scan prompt caps output at ~1000 tokens regardless of input size.

**Best input:** 15–20 URLs — homepage, pricing, each core feature page, docs overview, 1–2 comparison pages. Avoid blog posts (waste capacity on tactical content).

**For positioning docs, battlecards, research:** upload as PDF/PPTX/XLSX — processed more cleanly than URL scraping.

**Re-scanning** fully replaces the brief. Recalibrate when product positioning, pricing, or ICP changes.

---

### Hosting / Cloud Migration (Deferred)

| Issue | Severity | Fix |
|---|---|---|
| JSON files — no concurrent write safety | 🔴 High | `articles.json`, `sources.json`, `settings.json`, `kw_cache.json` have no locking. Migrate to SQLite or add file-level locks. Enable WAL mode on `insights.db`. |
| No authentication | 🔴 High | Anyone with URL can access keys and trigger Claude. Add HTTP basic auth as stopgap. |
| API keys in settings.json | 🟠 Medium | Move to server env vars on shared instance; show only set/not-set in UI. |
| Demo classifier hardcoded to localhost:8000 | 🟠 Medium | `demo_connector.py` calls `http://localhost:8000`. Co-deploy as second service, point via env var. |
| Ephemeral filesystem | 🟡 Low | `watch/`, `data/`, uploads need persistent disk. Use persistent volume. |

**Recommended path:** Railway with persistent volume. Classifier as second service in same project. HTTP basic auth for team access. ~1 day effort.

---

### Common Ops

```bash
# Start via launcher (preferred)
open "Content Engine.command"          # from inside storylane-content-engine/

# Manual start
cd storylane-content-engine && source venv/bin/activate && python3 app.py

# Kill if port is stuck
lsof -ti:8001 | xargs kill -9

# Tail logs (launcher mode)
tail -f /tmp/content-engine.log

# Check pipeline state
curl -s http://localhost:8001/api/status | python3 -m json.tool

# Export portable zip (Settings UI preferred, or:)
curl -o "Storylane Customer Insights Content Engine - Engine Only.zip" http://localhost:8001/api/export-zip
curl -o "Storylane Customer Insights Content Engine - Complete.zip" "http://localhost:8001/api/export-zip?bundle=1"
```
