# Storylane Customer Insights Content Engine

> **Start:** Double-click `Content Engine.command` — auto-creates venv, installs deps (shows a loading screen in the browser on first run), then opens `http://localhost:8001`
> **Port:** 8001 · Demo Classifier runs on 8000 — do not conflict
> **Sync to GitHub:** Double-click `Push to GitHub.command` — stages everything, asks for a description, commits and pushes (includes all data files: insights, articles, company brain, training formats)

---

## Working on This Codebase

This document is the single source of truth. Update it at the end of every session — built items come out of "What's Next", new modules and routes get added, nothing stale stays. Readable by a non-technical stakeholder at the top; a developer picking up cold at the bottom.

---

## What This Tool Is

Two tools, one repo, one workflow.

**Demo Classifier** (`storylane-demo-classifier/`, port 8000) — classifies and indexes Storylane demo URLs. The content engine queries it at draft time to embed real demo links into articles. Runs independently; engine falls back to local index scoring if classifier is offline.

**Content Engine** (`/`, port 8001) — turns customer call recordings and competitive intelligence into publication-ready SEO articles. Pipeline: ingest → topic generation → keyword research → draft → QC → SEO → export.

---

## For Non-Technical Readers

**Step by step:**

1. **Company Intelligence.** Scan Storylane's website and upload positioning docs (PDF/PPTX/XLSX). Everything distills into a compact brief that every draft draws from — so articles always write from Storylane's perspective.

2. **Customer Insights.** Grain and Sybill calls are pulled in automatically on a schedule. The system extracts pain points, objections, exact customer quotes, and metrics. You can also paste competitor data or any other text as a Thought Dump — it processes the same way.

3. **Topic Proposals.** Based on insight gaps and content pillar coverage, the system proposes article ideas — each with a specific angle, precise ideal reader, and the customer insights that inspired it.

4. **Keyword Research.** Three-pass: SERP analysis → parent-category brainstorm → Ahrefs validation. Manual keywords you paste in are always included, always ranked first.

5. **Draft Generation.** Uses company intelligence, directive customer insights (the ones that informed the topic), and real demo examples from the classifier. Two rules are hardcoded into every draft: Storylane appears as a genuine protagonist throughout (not a footnote), and every article ends with a closing CTA to storylane.io.

6. **QC + SEO.** 7-dimension QC check including insight authenticity — did the draft actually use real customer language? SEO checks keyword density, heading structure, internal links, missing subtopics. Both produce specific suggestions you accept or reject.

7. **Validation.** After applying suggestions, a fresh independent Claude pass checks insight fidelity, meaning preservation, and keyword coherence — no memory of the previous QC/SEO runs.

8. **Export.** Pre-export check: source customer quotes and metrics are verified still present in the final draft. If any were dropped or paraphrased away, flagged before download. Export as `.md`.

**Other things:**
- **Skills Library** — upload `.txt` or `.md` rubric files; each skill can be toggled on/off per use case (QC / SEO / Draft). Active skills are injected into the relevant prompt step.
- **Training Library** — add article URLs to teach the system writing formats per angle. Also accepts raw writing instructions (paste mode: "Writing instructions") — stored verbatim and injected as standing rules into every draft.
- **Manual keywords** — paste keywords in the keyword panel; always included regardless of Ahrefs scores.
- **Process All Ideas** — one button kicks off keyword research (+ full pipeline if auto-advance is on) for every idea-stage article.
- **Go back a stage** — step any article backward and re-run from there.
- **Auto-advance** — chain all pipeline stages automatically, with optional auto-apply of QC/SEO suggestions.
- **Export & Share** — one-click portable zip from Settings (API keys stripped). `?bundle=1` includes the Demo Classifier. First-run on recipient's machine shows a loading screen while dependencies install, then loads with all data intact.

---

## File Structure

```
storylane-content-engine/
├── app.py                        # Flask server + all routes (port 8001)
├── scheduler.py                  # APScheduler: insight scan every 6h, topic gen every 24h
├── config.py                     # All paths, API keys, reload_keys(), load_settings()
├── modules/
│   ├── model_manager.py          # create_message(tier) — Sonnet/Haiku with fallback chain
│   ├── company_brain.py          # URL scraping + file upload → company_brief.json
│   ├── insight_extractor.py      # Transcripts/dumps → structured insights → insights.db; archived flag; competitor SQL filter
│   ├── grain_connector.py        # Grain REST API v2 poller (cursor-paginated, filtered)
│   ├── sybill_connector.py       # Sybill REST API poller (mirrors grain pattern, pure pull)
│   ├── competitor_intel.py       # XLSX ingest: auto-detects sheets, groups by competitor, writes competitor_intel insights
│   ├── reddit_connector.py       # Reddit JSON API (no auth): fetch posts → extract_insights_from_text
│   ├── pillar_map.py             # Content pillar coverage map
│   ├── topic_planner.py          # Insights + pillar gaps → topic proposals with lineage
│   ├── keyword_researcher.py     # SERP → brainstorm → Ahrefs validate + manual keywords
│   ├── demo_connector.py         # POST /query-engine on classifier → local fallback
│   ├── draft_generator.py        # Directive insights + Yoda pre-filter + Storylane enforcement + CTA rule
│   ├── qc_engine.py              # 7-dimension scoring + suggestions + apply
│   ├── seo_engine.py             # Structural checks + Claude semantic pass + link check
│   ├── template_learner.py       # Article URL → structural template + mindset layer
│   ├── link_library.py           # Internal link index → injected into draft prompts
│   ├── skills_manager.py         # Skills rubric files: upload, store, toggle per use-case, inject
│   └── validation_engine.py      # Post-apply validation: insight fidelity + meaning + keywords
├── data/
│   ├── company_brief.json        # Distilled Storylane intelligence (~800–1200 tokens)
│   ├── insights.db               # SQLite: all extracted insights
│   ├── sources.json              # Processed source IDs — prevents re-processing
│   ├── articles.json             # All pipeline articles: { "articles": [...] }
│   ├── content_pillars.json      # Pillar coverage map
│   ├── content_formats.json      # Training library templates + writing instructions
│   ├── skills.json               # Uploaded skills rubrics (created on first upload)
│   ├── link_index.json           # Internal link library
│   ├── kw_cache.json             # Ahrefs keyword cache (gitignored)
│   ├── demo_query_cache.json     # Demo classifier query cache — 1h TTL (gitignored)
│   └── settings.json             # All config: API keys, scheduler, automation (gitignored)
├── watch/
│   ├── grain/                    # Drop Grain exports here for manual ingestion
│   └── sybill/                   # Drop Sybill exports here
├── output/articles/              # Exported .md files (gitignored)
├── storylane-demo-classifier/    # Demo Classifier — sibling tool in same repo, port 8000
├── Content Engine.command        # Launcher — auto-venv, first-run loading screen, opens browser
├── Push to GitHub.command        # git add . → commit (prompts for message) → push
└── static/index.html             # Full dashboard UI — all tabs, all JS, no build step
```

---

## API Routes

| Method | Route | What it does |
|---|---|---|
| GET | `/api/status` | Health + article counts by stage + scheduler log |
| GET | `/api/company-brief` | Returns company_brief.json |
| POST | `/api/company-brain/scan` | Scrapes URLs + processes docs → company_brief.json |
| POST | `/api/company-brain/upload` | Extracts text from uploaded file (PDF/PPTX/etc.) |
| GET | `/api/insights` | Insights with filters (source_type, tag, min_confidence, limit) |
| POST | `/api/insights/by_ids` | Fetch specific insights by UUID array |
| POST | `/api/insights/dump` | Text dump → extract_insights_from_text() |
| POST | `/api/insights/upload` | Uploaded transcript → extract_insights_from_text() |
| POST | `/api/scheduler/trigger` | `{ "job": "sybill_poll" }` manually kicks off a job |
| POST | `/api/watch/process` | Scans watch folders for new files |
| GET | `/api/watch/files` | Files currently in watch folders |
| GET | `/api/pillars` | Returns `{ map, gaps }` |
| POST | `/api/topics/generate` | Generates N topic proposals → articles at "idea" stage |
| GET | `/api/articles` | All articles (optional `?stage=` filter) |
| POST | `/api/articles` | Create article manually (backend ready; **UI button not yet built**) |
| GET | `/api/articles/:id` | Single article |
| PUT | `/api/articles/:id` | Update any article fields (stage rollback, etc.) |
| PUT | `/api/articles/:id/draft` | Save manual draft edits |
| POST | `/api/articles/:id/keywords` | Run keyword research → "keywords" stage |
| PUT | `/api/articles/:id/keywords/manual` | Save manually entered keywords |
| POST | `/api/articles/:id/draft` | Generate draft → "draft" stage |
| POST | `/api/articles/:id/qc` | Run QC pass → "qc" stage |
| POST | `/api/articles/:id/seo` | Run SEO pass → "seo" stage |
| POST | `/api/articles/:id/apply` | Apply accepted suggestions + auto-validate |
| POST | `/api/articles/:id/insight-check` | Pre-export: verify source quotes still in draft |
| POST | `/api/articles/:id/relink-insights` | Score + link most relevant insights retroactively |
| GET | `/api/articles/:id/export` | Download article as .md |
| POST | `/api/pipeline/process-all` | Batch keyword research on all idea-stage articles |
| GET | `/api/demo/search` | Search demo index |
| GET | `/api/demo/find` | Find best-fit demos for a topic/angle |
| POST | `/api/demo/deep-scan` | Trigger classifier deep scan |
| POST | `/api/training/add` | Add article URL to training library (async) |
| POST | `/api/training/add-text` | `{ text, label, angle_hint, mode }` — mode: `"analyze"` (extract template) or `"instructions"` (store raw as standing writing rules) |
| GET | `/api/training/formats` | List all learned formats + writing instructions |
| POST | `/api/training/formats/delete` | Delete a format/instructions entry |
| GET | `/api/skills` | List all uploaded skills |
| POST | `/api/skills/upload` | Upload `.txt`/`.md` rubric file — Haiku extracts rubric content async |
| PUT | `/api/skills/:id` | Toggle `active_qc`, `active_seo`, `active_draft` |
| DELETE | `/api/skills/:id` | Remove a skill |
| GET | `/api/links` | All indexed internal links |
| POST | `/api/links/add` | Scrape and index a URL (async) |
| POST | `/api/links/delete` | Remove a URL from the link index |
| GET | `/api/progress/:task_id` | Poll async task — resolves on `startsWith('DONE')` or `startsWith('ERROR')` |
| GET | `/api/settings` | Full settings |
| PUT | `/api/settings` | Save scheduler/automation/rubric/filter settings |
| PUT | `/api/settings/keys` | Save API keys (partial — only overwrites non-empty) |
| GET | `/api/export-zip` | Portable zip — `?bundle=1` includes Demo Classifier |
| POST | `/api/competitor-intel/upload` | Upload `.xlsx` competitor fact sheet — auto-detects sheets, one insight per competitor |
| POST | `/api/reddit/fetch` | `{ "query": "..." }` — fetch Reddit posts → extract insights async |
| POST | `/api/insights/archive` | Mark low-confidence insights (< 0.35) as archived |

All Claude-calling operations are async: return `{ task_id }` immediately, frontend polls `/progress/:task_id` every 1.2s.

---

## Key Module Notes

**`skills_manager.py`** — Skills are `.txt`/`.md` rubric files uploaded via the Skills tab. On upload, Haiku extracts the actionable rubric content, gives it a name, and suggests a use case. Each skill has three independent toggles (`active_qc`, `active_seo`, `active_draft`). `build_skills_block(use_case)` returns a formatted injection block for active skills; called by `draft_generator`, `qc_engine`, and `seo_engine`.

**`template_learner.py`** — Two entry paths: (1) URL → Sonnet extracts structural template + mindset layer, stored as `source_type: "url"` or `"text"`. (2) Raw instructions paste → stored verbatim as `source_type: "instructions"` with no analysis. In draft prompts: URL/text formats inject as format reference; instructions entries inject as `=== STANDING WRITING INSTRUCTIONS ===` blocks applied unconditionally.

**`draft_generator.py`** — Loads directive insights (from `article.insight_ids_used`) first, fills remaining slots (up to 8) with semantically relevant insights. Directive insights marked ★ with explicit instruction to use their language verbatim. Manual keywords always included. Standing writing instructions (from training library) + skills block (active draft skills) both injected before the main prompt. Two hardcoded rules: (7) Storylane as genuine protagonist throughout; (8) mandatory closing CTA H2 to storylane.io.

**`insight_extractor.py`** — SQLite per insight: `id, source_id, source_type, source_name, author, customer_segment, pain_points, quotes, objections, use_cases, metrics, competitors, tags, confidence, extracted_at, used_in, raw_summary, raw_input`. `used_in` is updated (not cleared) when an insight is referenced — same insight can be used across multiple articles. `ALTER TABLE` migration runs on init.

**`sybill_connector.py`** — Pure REST pull, no webhook/ngrok. Bearer `sk_live_...`. 90-day lookback on first run. Found 650+ historical calls. Rate-limited (0.3s / 0.5s delays). Wired to scheduler + manual poll button.

**`keyword_researcher.py`** — 3-pass: SERP → Haiku brainstorm from parent categories → Ahrefs validation. Manual keywords get `score += 1000` — always top the list, never filtered by KD/volume. `check_topics_demand(articles)` batch-checks Ahrefs volume + KD for idea-stage topics after generation and saves `demand_signal` onto each article.

**`demo_connector.py`** — Calls `/query-engine` on Demo Classifier (port 8000). Falls back to local `demo_index.json` scoring if classifier is offline. Classifier is a sibling tool in `storylane-demo-classifier/` within the same repo.

**`competitor_intel.py`** — Ingests XLSX competitor fact sheets. Auto-detects sheet type (multi-competitor sheets have a `Competitor` column; single-competitor sheets use the sheet name). Groups all data per competitor across all sheets, normalises name variants (e.g. `Tourial / Navless` → `Tourial`), then makes one Haiku call per competitor to produce a structured insight. `source_type: "competitor_intel"`. Deduplicates by `filename + competitor` — re-uploading the same file is a no-op; a second colleague's file creates additive insights. Requires pandas + openpyxl.

**`reddit_connector.py`** — Fetches Reddit posts using the public JSON API (no auth, no PRAW). Searches configurable subreddits (default: sales, marketing, SaaS, b2bmarketing, startups, salestechnology). Bundles top posts into text and passes to `extract_insights_from_text`. `source_type: "reddit"`. Each query gets a unique source_id (timestamp), so re-running the same query always creates a fresh insight.

**`draft_generator.py` (Yoda system)** — `_select_relevant_insights()` now extracts competitor names from the article topic (matched against a known list), runs a SQL-filtered query for competitor-specific insights first (`WHERE LOWER(competitors) LIKE '%howdygo%'`), then fills remaining slots from the general pool. General pool limit dropped from 100 to 50. Competitor insights always rank ahead of general insights at equal relevance scores.

---

## Article Data Schema

```json
{
  "id": "uuid",
  "topic": "...",
  "ideal_reader": "...",
  "angle": "thought_leadership | listicle | opinion | data_led | how_to | comparison | digital_pr",
  "pillar": "...",
  "strategic_intent": ["eeat_signal", "backlinkable", "primary_voice", "pillar_anchor"],
  "stage": "idea | keywords | draft | qc | seo | done",
  "structural_brief": "optional per-article writing instructions (overrides training library for this article)",
  "keywords": {
    "primary": { "keyword": "...", "volume": 0, "difficulty": 0, "source": "ahrefs|brainstorm|manual" },
    "secondary": [ "same shape" ],
    "manual": ["keyword one", "keyword two"],
    "serp": { "dominant_format": "...", "avg_word_count": 0, "featured_snippet": false }
  },
  "draft": "markdown string",
  "qc_result": { "scores": {}, "suggestions": [] },
  "seo_result": { "suggestions": [] },
  "validation_result": { "insight_fidelity": {}, "meaning_preservation": {}, "keyword_coherence": {}, "overall_verdict": "pass|warn|fail" },
  "social_signals": [ { "format": "carousel|post|poll", "hook": "..." } ],
  "insight_ids_used": ["uuid"],
  "overlap_warning": { "similar_to": "...", "reason": "..." },
  "demand_signal": { "volume": 0, "difficulty": 0, "checked_at": "ISO8601" },
  "created_at": "ISO8601",
  "updated_at": "ISO8601"
}
```

---

## Frontend Tab Structure

Single file: `static/index.html` — vanilla JS, no build step.

- `tab-dashboard` — pipeline flow visualization + scheduler status
- `tab-insights` — Active (unused) / Processed (used in articles) insight cards
- `tab-pipeline` — Kanban: Ideas → Keywords → Draft → QC → SEO → Done + batch controls
- `tab-editor` — Stage bar, rendered draft, QC / SEO / Validation / Info sidebar tabs
- `tab-training` — Training Library: URL queue + text paste (analyze or instructions mode)
- `tab-skills` — Skills Library: upload rubric files, toggle per QC / SEO / Draft
- `tab-settings` — Company scan, scheduler, automation, rubrics, keyword params, call filters, link library, API keys, models, Export & Share

---

## Integration Status

| Service | Status | Notes |
|---|---|---|
| Claude API | ✅ | All modules via `model_manager.create_message()` |
| Ahrefs v3 | ✅ | Keywords Explorer + SERP; cached in `kw_cache.json` |
| Demo Classifier | ✅ | `/query-engine` with local fallback; in same repo |
| Grain | ✅ | REST API v2 poller + watch folder + scheduler |
| Sybill | ✅ | REST API poller, no webhook needed, wired to scheduler |
| Webflow CMS | ⏳ Deferred | Push final articles via REST API v2 |

---

## What's Next to Build

| Item | Status | Complexity | Notes |
|---|---|---|---|
| **Image Generation step** | 🔲 Next | Large | Post-draft step: Claude reads the article, identifies sections that need visuals, generates self-contained HTML charts/graphics. Exports as high-fidelity screenshot. Waiting on skill file with exact HTML generation architecture before building. Placeholder button reserved in editor topbar. |
| **Webflow publish** | 🔲 Deferred | Medium | Push done articles to Webflow CMS via REST API v2. Needs collection ID + field mapping per pillar. |
| **Hosted deployment** | 🔲 Deferred | Medium | Railway. Persistent volume for data/, classifier as second service, HTTP basic auth for team access. ~1 day effort. |

## Recently Completed (2026-05-01)

| Item | Notes |
|---|---|
| **"Add Article" UI** | `+ Create Manually` button in Pipeline tab — topic, angle, pillar, ideal reader form. `ideal_reader` field added to create modal and passed through to article data. |
| **Competitor Intelligence ingest** | `modules/competitor_intel.py` — XLSX upload, auto-detects sheet types, merges name variants, one insight per competitor. `POST /api/competitor-intel/upload`. UI button in Insights tab. pandas + openpyxl added to venv. 13 insights pre-loaded from two colleague files without API (direct DB write). |
| **Smart insight retrieval (Yoda system)** | `archived` column added to insights.db. `get_insights()` now accepts `competitors` list → SQL pre-filter. `draft_generator.py` extracts competitor names from topic, pulls competitor-specific insights first, fills remaining slots from general pool. `POST /api/insights/archive` to trigger archival. |
| **Angle-to-SERP intent validation** | Rule-based mismatch warning in keyword panel: if `serp.dominant_format` doesn't match the article's `angle`, orange warning badge appears inline. No AI cost. |
| **Reddit insight source** | `modules/reddit_connector.py` — Reddit public JSON API (no OAuth, no PRAW). `POST /api/reddit/fetch`. UI button + modal in Insights tab. `source_type: "reddit"` in insights.db. Requires Claude API credits to extract insights. |
| **Search demand signal** | `check_topics_demand()` in `keyword_researcher.py` auto-runs after topic generation. Batch-fetches Ahrefs volume + KD for each proposed topic title, saves `demand_signal` onto article. Shown as 📊 badge on Kanban cards and in editor Info sidebar. No-ops if Ahrefs token absent. |

---

## Common Ops

```bash
# Start (preferred)
# Double-click Content Engine.command

# Sync data + code to GitHub
# Double-click Push to GitHub.command

# Manual start
cd storylane-content-engine && source venv/bin/activate && python3 app.py

# Tail logs
tail -f /tmp/content-engine.log

# Kill stuck port
lsof -ti:8001 | xargs kill -9

# Export zip (or use Settings UI)
curl -o engine.zip "http://localhost:8001/api/export-zip"
curl -o bundle.zip "http://localhost:8001/api/export-zip?bundle=1"
```
