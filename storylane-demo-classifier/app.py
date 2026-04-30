#!/usr/bin/env python3
"""
Storylane Demo Classifier -- Web Interface
==========================================
A browser-based UI for the persistent demo index.
Open http://localhost:8000 and explore, search, import, and classify demos.
"""

import asyncio
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

PORT = 8000
PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "output"
RUBRICS_DIR = PROJECT_DIR / "rubrics"
API_KEY_FILE = PROJECT_DIR / ".api_key"  # Local-only, gitignored


def _load_saved_api_key() -> str:
    """Load API key from local file if it exists."""
    if API_KEY_FILE.exists():
        try:
            return API_KEY_FILE.read_text().strip()
        except IOError:
            pass
    return ""


def _save_api_key_to_disk(key: str):
    """Persist API key to local file (readable only by user)."""
    try:
        API_KEY_FILE.write_text(key)
        API_KEY_FILE.chmod(0o600)
    except IOError as e:
        print(f"   Warning: Could not save API key to disk: {e}")

# Import index functions from run.py
sys.path.insert(0, str(PROJECT_DIR))
from run import (
    load_index, save_index, merge_demo_into_index, search_index, smart_query_index,
    filter_index, get_index_stats, import_urls_from_string, generate_report,
    SCAN_LAYERS, parse_query_intent, shortlist_candidates, build_query_plan,
    answer_from_existing, execute_layer_scans, has_layer,
    load_knowledge_base, generate_automated_suggestions, get_relevant_tips,
    enrich_demo_with_suggestions, check_missing_screenshots,
    load_query_knowledge, extract_query_themes,
    SCREENSHOTS_DIR, _safe_filename,
)

state = {
    "running": False,
    "process": None,
    "log_lines": [],
    "finished": False,
    "error": None,
    "active_rubric": None,
    "api_key": "",
}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._json_response(200, None, html=get_html())

        elif self.path == "/status":
            # Parse progress from log lines (Classifying... lines + demo counts)
            log = state["log_lines"]
            classified_count = sum(1 for l in log if "Classifying..." in l and "->" in l)
            # "   [N/M]" layer scan progress
            layer_progress = None
            for line in reversed(log):
                if line.strip().startswith("[") and "/" in line and "]" in line:
                    try:
                        chunk = line.strip().split("]")[0].lstrip("[")
                        cur, tot = chunk.split("/")
                        layer_progress = {"current": int(cur), "total": int(tot)}
                        break
                    except (ValueError, IndexError):
                        continue

            # Cost estimate: Haiku ~$0.001/demo text classification, Sonnet ~$0.02/demo w/screenshots
            # Very rough — based on log type lines
            sonnet_count = sum(1 for l in log if "Sonnet" in l and ("->" in l or "Re-classif" in l))
            haiku_count = max(0, classified_count - sonnet_count)
            estimated_cost = round(haiku_count * 0.001 + sonnet_count * 0.02, 3)

            self._json_response(200, {
                "running": state["running"],
                "finished": state["finished"],
                "error": state["error"],
                "log": state["log_lines"][-200:],
                "log_count": len(state["log_lines"]),
                "api_key_set": bool(state.get("api_key")),
                "progress": {
                    "classified": classified_count,
                    "layer": layer_progress,
                    "estimated_cost": estimated_cost,
                },
            })

        elif self.path == "/index":
            index = load_index()
            demos = list(index["demos"].values())
            demos.sort(key=lambda d: (
                -(d.get("classification", {}).get("overall_score", 0) or 0),
                d.get("name", "")
            ))
            # Annotate each demo with whether its screenshots are on disk
            for d in demos:
                if d.get("screenshots_captured"):
                    demo_dir = SCREENSHOTS_DIR / _safe_filename(d.get("name", ""))
                    d["screenshots_present"] = demo_dir.exists() and any(demo_dir.glob("*.png"))
                else:
                    d["screenshots_present"] = False
            self._json_response(200, {"demos": demos, "stats": get_index_stats(index)})

        elif self.path.startswith("/search?"):
            query = self.path.split("q=", 1)[1] if "q=" in self.path else ""
            query = query.split("&")[0]
            from urllib.parse import unquote
            query = unquote(query)
            index = load_index()
            results = search_index(index, query)
            self._json_response(200, {"demos": results, "query": query})

        elif self.path.startswith("/query?"):
            query = self.path.split("q=", 1)[1] if "q=" in self.path else ""
            query = query.split("&")[0]
            from urllib.parse import unquote
            query = unquote(query)
            index = load_index()
            api_key = state.get("api_key", "")
            results = smart_query_index(index, query, api_key=api_key)
            self._json_response(200, {"demos": results, "query": query, "mode": "smart"})

        elif self.path == "/stats":
            index = load_index()
            self._json_response(200, get_index_stats(index))

        elif self.path == "/results":
            json_path = OUTPUT_DIR / "demo_report.json"
            if json_path.exists():
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json_path.read_bytes())
            else:
                self._json_response(404, {"error": "No results yet"})

        elif self.path == "/rubric-status":
            self._json_response(200, {
                "status": state.get("rubric_status"),
                "error": state.get("rubric_error"),
                "rubric": state.get("rubric_text"),
                "active_rubric": state.get("active_rubric"),
            })

        elif self.path == "/default-rubric":
            criteria_path = PROJECT_DIR / "classification_criteria.txt"
            rubric = criteria_path.read_text() if criteria_path.exists() else "(built-in default)"
            self._json_response(200, {"rubric": rubric})

        elif self.path == "/download-csv":
            csv_path = OUTPUT_DIR / "demo_report.csv"
            if csv_path.exists():
                self.send_response(200)
                self.send_header("Content-type", "text/csv")
                self.send_header("Content-Disposition", "attachment; filename=demo_report.csv")
                self.end_headers()
                self.wfile.write(csv_path.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()

        elif self.path == "/layers":
            self._json_response(200, {
                "layers": {
                    name: {
                        "label": l["label"],
                        "description": l["description"],
                        "needs_screenshots": l["needs_screenshots"],
                        "tier": l["tier"],
                    }
                    for name, l in SCAN_LAYERS.items()
                }
            })

        elif self.path == "/download-json":
            index = load_index()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Content-Disposition", "attachment; filename=demo_index.json")
            self.end_headers()
            self.wfile.write(json.dumps(list(index["demos"].values()), indent=2).encode())

        elif self.path == "/tips":
            kb = load_knowledge_base()
            self._json_response(200, {
                "tips": kb.get("tips", []),
                "categories": kb.get("tip_categories", []),
                "suggestions_rules": kb.get("suggestions", []),
                "tips_count": kb.get("tips_count", 0),
            })

        elif self.path.startswith("/tips?"):
            from urllib.parse import unquote
            cat = self.path.split("category=", 1)[1] if "category=" in self.path else ""
            cat = unquote(cat.split("&")[0])
            kb = load_knowledge_base()
            tips = kb.get("tips", [])
            if cat:
                tips = [t for t in tips if t.get("category", "").lower() == cat.lower()]
            self._json_response(200, {"tips": tips, "category": cat})

        elif self.path.startswith("/demo-suggestions/"):
            # Get suggestions for a specific demo by key
            from urllib.parse import unquote
            key = unquote(self.path.split("/demo-suggestions/", 1)[1])
            index = load_index()
            demo = index["demos"].get(key)
            if demo:
                suggestions = generate_automated_suggestions(demo)
                tips = get_relevant_tips(demo)
                self._json_response(200, {
                    "demo": demo.get("name", "?"),
                    "suggestions": suggestions,
                    "relevant_tips": [
                        {"tip": t["tip"], "category": t["category"],
                         "company": t.get("company", ""), "demo_link": t.get("demo_link", ""),
                         "reasoning": t.get("reasoning", "")}
                        for t in tips
                    ],
                })
            else:
                self._json_response(404, {"error": "Demo not found"})

        elif self.path == "/knowledge":
            knowledge = load_query_knowledge()
            self._json_response(200, {
                "total_queries": len(knowledge.get("queries", [])),
                "themes_learned": list(knowledge.get("theme_index", {}).keys()),
                "queries": knowledge.get("queries", []),
                "theme_index": knowledge.get("theme_index", {}),
            })

        elif self.path == "/screenshot-status":
            index = load_index()
            missing = check_missing_screenshots(index)
            self._json_response(200, {"missing": missing, "count": len(missing)})

        elif self.path.startswith("/export-zip"):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            include_screenshots = qs.get("screenshots", ["0"])[0] == "1"
            self._serve_export_zip(include_screenshots)

        else:
            self.send_response(404)
            self.end_headers()

    def _serve_export_zip(self, include_screenshots: bool):
        EXCLUDE_DIRS = {"venv", "__pycache__", "output", ".git"}

        mac_setup = """\
#!/bin/bash
# Right-click this file → Open → Open (once only).
# Clears macOS quarantine so Start Classifier.command double-clicks normally.
#
# Prefer Terminal? Paste this instead (adjust path as needed):
#   xattr -dr com.apple.quarantine ~/Downloads/storylane-demo-classifier

cd "$(dirname "$0")"
echo ""
echo "🔓 Clearing macOS security flags..."
xattr -dr com.apple.quarantine . 2>/dev/null
chmod +x "Start Classifier.command" 2>/dev/null
echo "✅ Done! Double-click Start Classifier.command to launch."
echo ""
echo "Press any key to close..."
read -n 1
"""

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in PROJECT_DIR.rglob("*"):
                # Skip excluded top-level dirs
                parts = item.relative_to(PROJECT_DIR).parts
                if parts[0] in EXCLUDE_DIRS:
                    continue
                # Skip .api_key
                if item.name == ".api_key":
                    continue
                # Skip screenshots if not including them
                if not include_screenshots and parts[0] == "screenshots":
                    continue
                if item.is_file():
                    zf.write(item, item.relative_to(PROJECT_DIR))
            # Always include empty screenshots folder so the path exists
            if not include_screenshots:
                zf.writestr("screenshots/.keep", "")
            # Mac quarantine fix script
            zf.writestr("▶ Start Here — Mac Setup.command", mac_setup)
        buf.seek(0)
        data = buf.read()
        fname = "storylane-demo-classifier-with-screenshots.zip" if include_screenshots else "storylane-demo-classifier.zip"
        self.send_response(200)
        self.send_header("Content-type", "application/zip")
        self.send_header("Content-Disposition", f"attachment; filename={fname}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self._read_body()

        if self.path == "/start":
            if state["running"]:
                self._json_response(400, {"error": "Already running"})
                return

            limit = body.get("limit", 0)
            no_classify = body.get("no_classify", False)
            mode = body.get("mode", "fast")
            extra_urls = body.get("extra_urls", "")
            rescan = body.get("rescan", False)
            rescan_query = body.get("rescan_query", "")
            rescan_keys = body.get("rescan_keys", "")
            criteria_file = state.get("active_rubric")
            api_key = state.get("api_key", "")

            if not api_key and not no_classify:
                self._json_response(400, {"error": "No API key set. Please enter your Anthropic API key first."})
                return

            state["running"] = True
            state["finished"] = False
            state["error"] = None
            state["log_lines"] = []

            thread = threading.Thread(
                target=run_classifier,
                args=(limit, no_classify, mode, criteria_file, extra_urls, api_key, rescan, rescan_query, rescan_keys),
                daemon=True
            )
            thread.start()
            self._json_response(200, {"status": "started"})

        elif self.path == "/stop":
            if state["process"]:
                state["process"].terminate()
                state["log_lines"].append("Stopped by user -- partial results have been saved")
            state["running"] = False
            state["finished"] = True
            self._json_response(200, {"status": "stopped"})

        elif self.path == "/import-urls":
            url_text = body.get("urls", "").strip()
            tags = body.get("tags", [])
            if not url_text:
                self._json_response(400, {"error": "No URLs provided"})
                return

            urls = import_urls_from_string(url_text)
            if not urls:
                self._json_response(400, {"error": "No valid URLs found"})
                return

            index = load_index()
            added = 0
            for url in urls:
                name = url.rstrip("/").split("/")[-1] or "imported-demo"
                name = name.replace("-", " ").replace("_", " ").title()
                entry = {"name": name, "source": "import"}
                if tags:
                    entry["tags"] = tags
                if "/demo/" in url:
                    entry["demo_url"] = url
                else:
                    entry["showcase_url"] = url
                was_new = merge_demo_into_index(index, entry)
                if was_new:
                    added += 1

            save_index(index)
            generate_report(index)
            self._json_response(200, {
                "status": "imported",
                "added": added,
                "total": len(urls),
                "already_existed": len(urls) - added,
                "index_size": len(index["demos"]),
            })

        elif self.path == "/tag":
            demo_key = body.get("key", "")
            tags = body.get("tags", [])
            if not demo_key:
                self._json_response(400, {"error": "No demo key provided"})
                return
            index = load_index()
            if demo_key in index["demos"]:
                existing = set(index["demos"][demo_key].get("tags", []))
                existing.update(tags)
                index["demos"][demo_key]["tags"] = sorted(existing)
                save_index(index)
                self._json_response(200, {"status": "tagged", "tags": index["demos"][demo_key]["tags"]})
            else:
                self._json_response(404, {"error": "Demo not found in index"})

        elif self.path == "/remove-tag":
            demo_key = body.get("key", "")
            tag = body.get("tag", "")
            if not demo_key or not tag:
                self._json_response(400, {"error": "Missing key or tag"})
                return
            index = load_index()
            if demo_key in index["demos"]:
                tags = index["demos"][demo_key].get("tags", [])
                if tag in tags:
                    tags.remove(tag)
                index["demos"][demo_key]["tags"] = tags
                save_index(index)
                self._json_response(200, {"status": "removed", "tags": tags})
            else:
                self._json_response(404, {"error": "Demo not found"})

        elif self.path == "/query-engine":
            query = body.get("query", "").strip()
            if not query:
                self._json_response(400, {"error": "No query provided"})
                return
            api_key = state.get("api_key", "")
            if not api_key:
                self._json_response(400, {"error": "API key required for queries"})
                return

            # Run query pipeline synchronously (fast -- mostly index lookups)
            try:
                index = load_index()
                intent = parse_query_intent(query, api_key)
                candidates = shortlist_candidates(index, intent)
                plan = build_query_plan(index, intent, candidates)

                # Get answer from existing data
                loop = asyncio.new_event_loop()
                answer = loop.run_until_complete(
                    answer_from_existing(query, candidates[:20], intent, api_key)
                )
                loop.close()

                # Extract themes for this query (reuse from answer_from_existing)
                try:
                    themes = extract_query_themes(query, api_key)
                except Exception:
                    themes = []

                # Load knowledge to show learning status
                knowledge = load_query_knowledge()

                self._json_response(200, {
                    "query": query,
                    "intent": intent,
                    "answer": answer,
                    "themes": themes,
                    "knowledge_status": {
                        "total_queries_learned": len(knowledge.get("queries", [])),
                        "themes_known": list(knowledge.get("theme_index", {}).keys()),
                    },
                    "candidates": [
                        {
                            "name": d.get("name"),
                            "display_name": d.get("display_name") or d.get("name"),
                            "key": d.get("key"),
                            "classification": d.get("classification", {}),
                            "tags": d.get("tags", []),
                            "insights": d.get("insights", {}),
                            "layers": list(d.get("layers", {}).keys()),
                            "demo_url": d.get("demo_url", ""),
                        }
                        for d in candidates[:20]
                    ],
                    "plan": {
                        "total_candidates": plan["total_candidates"],
                        "already_have_data": plan["already_have_data"],
                        "need_layer_scan": plan["need_layer_scan"],
                        "need_base_scan": plan["need_base_scan"],
                        "total_estimated_cost": plan["total_estimated_cost"],
                        "layer_costs": plan["layer_costs"],
                        "required_layers": intent.get("required_layers", []),
                    },
                })
            except Exception as e:
                self._json_response(500, {"error": str(e)})

        elif self.path == "/run-scan":
            layer_names = body.get("layers", [])
            demo_keys = body.get("demo_keys", [])
            limit = body.get("limit", 20)
            query_context = body.get("query_context", None)  # From query-triggered scans
            api_key = state.get("api_key", "")

            if not api_key:
                self._json_response(400, {"error": "API key required"})
                return
            if not layer_names:
                self._json_response(400, {"error": "No layers specified"})
                return

            # Run scan in background thread
            if state["running"]:
                self._json_response(400, {"error": "A scan is already running"})
                return

            state["running"] = True
            state["finished"] = False
            state["error"] = None
            state["log_lines"] = []

            def do_scan():
                try:
                    index = load_index()
                    if demo_keys:
                        demos = [index["demos"][k] for k in demo_keys if k in index["demos"]]
                    else:
                        demos = [d for d in index["demos"].values() if d.get("steps_text")]
                    demos = demos[:limit]

                    loop = asyncio.new_event_loop()
                    scanned = loop.run_until_complete(
                        execute_layer_scans(index, demos, layer_names, api_key,
                                            query_context=query_context)
                    )
                    loop.close()
                    generate_report(index)
                    ctx_note = f" (query-focused: \"{query_context[:50]}\")" if query_context else ""
                    state["log_lines"].append(f"Scanned {scanned} demo-layers{ctx_note}. Index updated.")
                    state["finished"] = True
                except Exception as e:
                    state["error"] = str(e)
                    state["finished"] = True
                finally:
                    state["running"] = False

            threading.Thread(target=do_scan, daemon=True).start()
            self._json_response(200, {"status": "scanning"})

        elif self.path == "/upload-framework":
            doc_text = body.get("doc_text", "").strip()
            if not doc_text:
                self._json_response(400, {"error": "No document text provided"})
                return

            def do_generate():
                try:
                    state["rubric_status"] = "generating"
                    state["rubric_error"] = None
                    from run import generate_rubric_from_doc
                    RUBRICS_DIR.mkdir(parents=True, exist_ok=True)
                    timestamp = int(time.time())
                    rubric_path = RUBRICS_DIR / f"custom_rubric_{timestamp}.txt"
                    rubric_text = generate_rubric_from_doc(doc_text, output_path=rubric_path, api_key=state.get("api_key", ""))
                    state["active_rubric"] = str(rubric_path)
                    state["rubric_text"] = rubric_text
                    state["rubric_status"] = "ready"
                except Exception as e:
                    state["rubric_error"] = str(e)
                    state["rubric_status"] = "error"

            state["rubric_status"] = "generating"
            threading.Thread(target=do_generate, daemon=True).start()
            self._json_response(200, {"status": "generating"})

        elif self.path == "/reset-rubric":
            state["active_rubric"] = None
            state["rubric_text"] = None
            state["rubric_status"] = None
            self._json_response(200, {"status": "reset"})

        elif self.path == "/retrieve-screenshots":
            demo_keys = body.get("demo_keys", [])
            if not demo_keys:
                self._json_response(400, {"error": "No demo keys provided"})
                return
            if state["running"]:
                self._json_response(400, {"error": "A scan is already running"})
                return

            state["running"] = True
            state["finished"] = False
            state["error"] = None
            state["log_lines"] = []

            def do_retrieve():
                try:
                    venv_python = str(PROJECT_DIR / "venv" / "bin" / "python3")
                    if not Path(venv_python).exists():
                        venv_python = sys.executable
                    keys_str = ",".join(demo_keys)
                    cmd = [venv_python, "-u", str(PROJECT_DIR / "run.py"),
                           "--retrieve-screenshots", keys_str]
                    env = os.environ.copy()
                    env["PYTHONUNBUFFERED"] = "1"
                    state["log_lines"].append(f"Retrieving screenshots for {len(demo_keys)} demo(s)...")
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1, env=env, cwd=str(PROJECT_DIR),
                    )
                    state["process"] = proc
                    for line in proc.stdout:
                        state["log_lines"].append(line.rstrip("\n"))
                    proc.wait()
                    state["process"] = None
                    state["finished"] = True
                except Exception as e:
                    state["error"] = str(e)
                    state["finished"] = True
                finally:
                    state["running"] = False

            threading.Thread(target=do_retrieve, daemon=True).start()
            self._json_response(200, {"status": "retrieving", "count": len(demo_keys)})

        elif self.path == "/save-api-key":
            key = body.get("api_key", "").strip()
            if not key:
                self._json_response(400, {"error": "No API key provided"})
                return
            state["api_key"] = key
            _save_api_key_to_disk(key)
            self._json_response(200, {"status": "saved"})

        else:
            self.send_response(404)
            self.end_headers()

    def _read_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length).decode() if content_length else "{}"
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def _json_response(self, code, data, html=None):
        self.send_response(code)
        if html:
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())
        else:
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass


def run_classifier(limit, no_classify, mode="fast", criteria_file=None, extra_urls="", api_key="", rescan=False, rescan_query="", rescan_keys=""):
    try:
        venv_python = str(PROJECT_DIR / "venv" / "bin" / "python3")
        if not Path(venv_python).exists():
            venv_python = sys.executable

        cmd = [venv_python, "-u", str(PROJECT_DIR / "run.py")]
        if limit:
            cmd += ["--limit", str(limit)]
        if no_classify:
            cmd += ["--no-classify"]
        if mode:
            cmd += ["--mode", mode]
        if criteria_file:
            cmd += ["--criteria-file", criteria_file]
        if extra_urls:
            cmd += ["--extra-urls", extra_urls]
        if api_key:
            cmd += ["--api-key", api_key]
        if rescan_keys:
            cmd += ["--rescan-keys", rescan_keys]
        elif rescan:
            if rescan_query:
                cmd += ["--rescan", rescan_query]
            else:
                cmd += ["--rescan"]

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        state["log_lines"].append(f"Starting classifier pipeline...")

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env, cwd=str(PROJECT_DIR),
        )
        state["process"] = proc

        for line in proc.stdout:
            state["log_lines"].append(line.rstrip("\n"))

        proc.wait()
        state["process"] = None

        if proc.returncode != 0:
            state["error"] = f"Process exited with code {proc.returncode}"
        state["finished"] = True

    except Exception as e:
        state["error"] = str(e)
        state["finished"] = True
    finally:
        state["running"] = False


def get_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Storylane Demo Classifier</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f0a1a;
    color: #e0dce8;
    min-height: 100vh;
  }
  .container { max-width: 1100px; margin: 0 auto; padding: 32px 20px; }

  h1 {
    font-size: 1.8rem;
    background: linear-gradient(135deg, #f0a 0%, #fa0 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 6px;
  }
  .subtitle { color: #8a8494; margin-bottom: 24px; font-size: 0.9rem; }

  /* Tabs */
  .tabs { display: flex; gap: 0; margin-bottom: 24px; border-bottom: 1px solid #2a2438; }
  .tab {
    padding: 10px 20px; cursor: pointer; font-size: 0.9rem; font-weight: 500;
    color: #8a8494; border-bottom: 2px solid transparent; transition: all 0.2s;
    background: none; border-top: none; border-left: none; border-right: none;
  }
  .tab:hover { color: #c8c0d8; }
  .tab.active { color: #f0a; border-bottom-color: #f0a; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  .panel {
    background: #1a1428; border-radius: 12px; padding: 20px;
    margin-bottom: 20px; border: 1px solid #2a2438;
  }
  .panel h2 { font-size: 1rem; margin-bottom: 14px; color: #c8c0d8; }

  /* Stats cards */
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .stat-card {
    background: #1a1428; border: 1px solid #2a2438; border-radius: 10px; padding: 14px 16px; text-align: center;
  }
  .stat-card .value { font-size: 1.5rem; font-weight: 700; color: #f0a; }
  .stat-card .label { font-size: 0.75rem; color: #8a8494; margin-top: 4px; }
  .stat-card .sublabel { font-size: 0.68rem; color: #5a5468; margin-top: 2px; }
  .stat-card.depth-light .value { color: #8af; }
  .stat-card.depth-full .value { color: #8fd; }

  /* Search */
  .search-bar {
    display: flex; gap: 10px; margin-bottom: 20px;
  }
  .search-bar input {
    flex: 1; background: #1a1428; border: 1px solid #3a3448; color: #e0dce8;
    padding: 10px 14px; border-radius: 8px; font-size: 0.9rem;
  }
  .search-bar input:focus { border-color: #f0a; outline: none; }
  .search-bar input::placeholder { color: #5a5468; }

  /* Demo tiles */
  .demo-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
  }
  @media (max-width: 900px) { .demo-grid { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 580px) { .demo-grid { grid-template-columns: 1fr; } }

  .demo-card {
    background: #1a1428; border: 1px solid #2a2438; border-radius: 10px; padding: 14px;
    transition: border-color 0.2s; display: flex; flex-direction: column; gap: 8px;
  }
  .demo-card:hover { border-color: #3a3458; }
  .demo-card .tile-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 6px; }
  .demo-card .name { font-weight: 600; font-size: 0.88rem; line-height: 1.3; flex: 1;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .demo-card .tile-score { font-size: 1.2rem; font-weight: 700; color: #f0a; white-space: nowrap; flex-shrink: 0; }
  .demo-card .meta { font-size: 0.72rem; color: #5a5468; }
  .demo-card .badge {
    font-size: 0.72rem; padding: 2px 7px; border-radius: 4px; display: inline-block;
    font-weight: 500;
  }
  .badge-strong { background: #1a3028; color: #8fd; }
  .badge-weak { background: #3a2820; color: #fa8; }
  .badge-generic { background: #2a2838; color: #aaf; }
  .badge-click { background: #2a2428; color: #aaa; }
  .badge-gated { background: #3a2030; color: #f8a; }
  .badge-unscanned { background: #2a2438; color: #666; }
  .badge-source { background: #1a2028; color: #8af; font-size: 0.7rem; }

  .score-row { display: flex; gap: 12px; margin: 8px 0; flex-wrap: wrap; }
  .score-pill {
    font-size: 0.78rem; padding: 2px 8px; border-radius: 10px; background: #0f0a1a;
    border: 1px solid #2a2438;
  }
  .summary-text { font-size: 0.82rem; color: #a8a0b8; line-height: 1.5; margin-top: 6px; }

  .tag-list { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 6px; }
  .tag {
    font-size: 0.72rem; padding: 2px 8px; border-radius: 10px;
    background: #2a1838; color: #c8a0e8; cursor: default;
  }
  .tag .x { cursor: pointer; margin-left: 4px; color: #f88; }
  .tag .x:hover { color: #faa; }

  .demo-card .actions { display: flex; gap: 6px; margin-top: 8px; }

  /* Buttons */
  .btn {
    padding: 8px 16px; border: none; border-radius: 8px; font-size: 0.85rem;
    font-weight: 500; cursor: pointer; transition: all 0.2s;
  }
  .btn-primary { background: linear-gradient(135deg, #f0a 0%, #fa0 100%); color: #0f0a1a; }
  .btn-primary:hover { opacity: 0.9; }
  .btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-secondary { background: #2a2438; color: #c8c0d8; }
  .btn-secondary:hover { background: #3a3448; }
  .btn-danger { background: #3a2030; color: #f88; }
  .btn-danger:hover { background: #4a2840; }
  .btn-small { padding: 5px 12px; font-size: 0.78rem; }
  .btn-download { background: #1a2830; color: #8fd; }
  .btn-download:hover { background: #2a3840; }

  /* Form elements */
  select, input[type="text"], input[type="password"], textarea {
    background: #0f0a1a; border: 1px solid #3a3448; color: #e0dce8;
    padding: 8px 12px; border-radius: 8px; font-size: 0.85rem;
    font-family: inherit;
  }
  select:focus, input:focus, textarea:focus { border-color: #f0a; outline: none; }
  textarea { width: 100%; min-height: 100px; resize: vertical; line-height: 1.5; }

  .form-row { display: flex; gap: 12px; align-items: end; flex-wrap: wrap; }
  .form-group { display: flex; flex-direction: column; gap: 4px; }
  .form-group label { font-size: 0.8rem; color: #8a8494; }

  /* Log panel */
  .log-panel {
    background: #0a0714; border-radius: 10px; padding: 16px; margin-bottom: 20px;
    border: 1px solid #2a2438; max-height: 400px; overflow-y: auto;
    font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.78rem; line-height: 1.6;
  }
  .log-panel .line { white-space: pre-wrap; word-break: break-word; }

  .status-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #3a3448; }
  .status-dot.running { background: #f0a; animation: pulse 1.5s infinite; }
  .status-dot.done { background: #8fd; }
  .status-dot.error { background: #f88; }
  .status-text { font-size: 0.82rem; color: #8a8494; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

  /* Modal */
  .modal-overlay {
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.7); backdrop-filter: blur(4px);
    display: flex; align-items: center; justify-content: center; z-index: 1000;
  }
  .modal-overlay.hidden { display: none; }
  .modal {
    background: #1a1428; border: 1px solid #3a2848; border-radius: 14px;
    padding: 32px; max-width: 500px; width: 90%;
  }
  .modal h2 {
    font-size: 1.3rem;
    background: linear-gradient(135deg, #f0a 0%, #fa0 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 10px;
  }
  .modal p { color: #a8a0b8; font-size: 0.85rem; line-height: 1.5; margin-bottom: 14px; }
  .modal .btn-row { display: flex; gap: 10px; justify-content: flex-end; margin-top: 16px; }
  .modal .error-msg { color: #f88; font-size: 0.78rem; margin-bottom: 6px; display: none; }

  .api-badge {
    display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px;
    border-radius: 14px; font-size: 0.75rem; cursor: pointer;
  }
  .api-badge.set { background: #1a3028; color: #8fd; }
  .api-badge.unset { background: #3a2030; color: #f88; }

  /* Bulk import area */
  .import-area { margin-top: 12px; }
  .import-area textarea {
    width: 100%; min-height: 80px; margin-bottom: 8px;
    font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.8rem;
  }
  .import-result { font-size: 0.82rem; color: #8fd; margin-top: 8px; }

  /* Filter bar */
  .filter-bar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .demo-card.selected { outline: 2px solid #7c3aed; }
  .tile-select-cb { position:absolute; top:10px; right:10px; width:18px; height:18px; cursor:pointer; accent-color:#7c3aed; }
  .demo-card { position: relative; }
  .filter-chip {
    padding: 4px 12px; border-radius: 14px; font-size: 0.78rem;
    background: #2a2438; color: #8a8494; cursor: pointer; border: 1px solid transparent;
    transition: all 0.15s;
  }
  .filter-chip:hover { background: #3a3448; }
  .filter-chip.active { background: #2a1838; color: #f0a; border-color: #f0a; }

  .count-badge {
    font-size: 0.7rem; background: #0f0a1a; padding: 1px 6px;
    border-radius: 8px; margin-left: 4px;
  }

  /* Collapsible */
  .collapsible-header {
    display: flex; align-items: center; justify-content: space-between;
    cursor: pointer; user-select: none;
  }
  .collapsible-header .toggle { font-size: 0.78rem; color: #5a5468; }
  .collapsible-body { max-height: 0; overflow: hidden; transition: max-height 0.3s ease; }
  .collapsible-body.open { max-height: 2000px; }

  .rubric-preview {
    background: #0a0714; border-radius: 8px; padding: 14px; max-height: 200px;
    overflow-y: auto; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.75rem;
    line-height: 1.5; color: #a8a0b8; white-space: pre-wrap; border: 1px solid #2a2438;
  }
  .spinner {
    display: inline-block; width: 14px; height: 14px;
    border: 2px solid #3a3448; border-top-color: #f0a;
    border-radius: 50%; animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .empty-state {
    text-align: center; padding: 40px 20px; color: #5a5468;
    grid-column: 1 / -1;
  }
  .empty-state .icon { font-size: 2rem; margin-bottom: 12px; }
  .empty-state p { font-size: 0.9rem; margin-bottom: 16px; }

  .badge-photo-missing { background: #2a1a10; color: #f8a; font-size: 0.7rem; }
  .badge-depth-light { background: #1a2030; color: #8af; font-size: 0.68rem; }
  .badge-depth-full  { background: #1a3028; color: #8fd; font-size: 0.68rem; }
  .badge-chapters    { background: #281a38; color: #c9a0f8; font-size: 0.68rem; }

  .cost-estimate {
    background: #0f0a1a; border: 1px solid #2a2438; border-radius: 8px;
    padding: 12px 16px; margin-top: 12px; font-size: 0.82rem;
    display: flex; gap: 24px; flex-wrap: wrap; align-items: center;
  }
  .cost-estimate .est-item { display: flex; flex-direction: column; gap: 2px; }
  .cost-estimate .est-val { font-weight: 600; color: #f0a; font-size: 0.95rem; }
  .cost-estimate .est-lbl { color: #5a5468; font-size: 0.72rem; }

  .share-btn-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
  .btn-share { background: #1a2030; color: #8af; border: 1px solid #2a3448; }
  .btn-share:hover { background: #243040; }

  /* Screenshot retrieval modal */
  #screenshotModal .modal { max-width: 520px; }
  #screenshotModal .missing-list {
    max-height: 160px; overflow-y: auto; margin: 10px 0;
    background: #0a0714; border-radius: 8px; padding: 10px 14px;
    font-size: 0.8rem; color: #a8a0b8; line-height: 1.8;
  }
</style>
</head>
<body>

<!-- API Key Modal -->
<div class="modal-overlay" id="introModal">
  <div class="modal">
    <h2>Storylane Demo Classifier</h2>
    <p>Build a persistent index of Storylane customer demos. Scrape, classify, search, and import demos -- each scan adds to your knowledge base.</p>
    <label style="display:block; font-size:0.8rem; color:#8a8494; margin-bottom:6px;">Anthropic API Key</label>
    <input type="password" id="apiKeyInput" placeholder="sk-ant-..." style="width:100%; margin-bottom:4px;" onkeydown="if(event.key==='Enter')saveApiKey()">
    <span style="font-size:0.75rem; color:#5a5468;">
      <a href="https://console.anthropic.com/settings/keys" target="_blank" style="color:#fa8; text-decoration:none;">Get your API key</a>
    </span>
    <div class="error-msg" id="apiKeyError"></div>
    <div class="btn-row">
      <button class="btn btn-secondary" onclick="skipApiKey()">Skip (browse only)</button>
      <button class="btn btn-primary" onclick="saveApiKey()">Save & Continue</button>
    </div>
  </div>
</div>

<!-- Screenshot Retrieval Modal -->
<div class="modal-overlay hidden" id="screenshotModal">
  <div class="modal">
    <h2>Missing Screenshots</h2>
    <p id="screenshotModalDesc" style="color:#a8a0b8; font-size:0.85rem; line-height:1.5; margin-bottom:10px;"></p>
    <div class="missing-list" id="screenshotMissingList"></div>
    <p style="font-size:0.82rem; color:#5a5468; margin-top:8px;">
      Retrieving re-walks each demo live with Playwright to recapture screenshots.<br>
      Skipping uses text-only for this scan — you can retrieve screenshots anytime later.
    </p>
    <div class="btn-row">
      <button class="btn btn-secondary" onclick="proceedTextOnly()">Skip, use text-only</button>
      <button class="btn btn-primary" onclick="retrieveScreenshotsThenScan()">Retrieve Screenshots</button>
    </div>
  </div>
</div>

<!-- Main App -->
<div class="container">
  <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;">
    <h1>Demo Classifier</h1>
    <div style="display:flex; gap:8px; align-items:center;">
      <span id="progressBar" style="display:none; background:#1a2830; color:#8fd; padding:4px 10px; border-radius:12px; font-size:0.75rem; align-items:center; gap:6px;"></span>
      <span class="api-badge unset" id="apiBadge" onclick="showApiKeyModal()">No API Key</span>
    </div>
  </div>
  <p class="subtitle">Persistent demo index -- search, classify, and explore Storylane customer demos</p>

  <!-- Stats Row -->
  <div class="stats-row" id="statsRow">
    <div class="stat-card"><div class="value" id="statTotal">--</div><div class="label">Total Demos</div></div>
    <div class="stat-card depth-light"><div class="value" id="statLight">--</div><div class="label">Light Scan</div><div class="sublabel">Haiku · text only</div></div>
    <div class="stat-card depth-full"><div class="value" id="statFull">--</div><div class="label">Full Scan</div><div class="sublabel">Sonnet · screenshots</div></div>
    <div class="stat-card"><div class="value" id="statUnscanned">--</div><div class="label">Not Scanned</div></div>
    <div class="stat-card"><div class="value" id="statAvg">--</div><div class="label">Avg Score</div></div>
    <div class="stat-card"><div class="value" id="statTop">--</div><div class="label">Top Score</div></div>
  </div>

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab active" onclick="switchTab('browse')">Demo Library</button>
    <button class="tab" onclick="switchTab('query')">Ask Demo Agent</button>
    <button class="tab" onclick="switchTab('scan')">Scan & Classify</button>
    <button class="tab" onclick="switchTab('import')">Import</button>
    <button class="tab" onclick="switchTab('tips')">Tips & Tricks</button>
    <button class="tab" onclick="switchTab('settings')">Settings</button>
  </div>

  <!-- TAB: Browse Index -->
  <div class="tab-content active" id="tab-browse">
    <div class="search-bar">
      <input type="text" id="searchInput" placeholder="Search demos by name, category, type, tags..." oninput="debounceSearch()" onkeydown="if(event.key==='Enter' && event.shiftKey) smartQuery()">
      <button class="btn btn-secondary btn-small" onclick="smartQuery()" title="AI-powered search (uses API credits)">Smart Search</button>
    </div>
    <div style="font-size:0.72rem; color:#5a5468; margin-top:-16px; margin-bottom:12px;">
      Type to filter instantly. Press Shift+Enter or click "Smart Search" for AI-powered natural language queries.
    </div>

    <div style="display:flex; gap:8px; align-items:center; margin-bottom:8px; flex-wrap:wrap;">
      <div class="filter-bar" id="filterBar" style="margin-bottom:0; flex:1;"></div>
      <button class="btn btn-secondary btn-small" id="selectModeBtn" onclick="toggleSelectMode()" style="white-space:nowrap;">Select</button>
    </div>

    <div id="selectionBar" style="display:none; background:#1e1830; border:1px solid #7c3aed; border-radius:8px; padding:10px 14px; margin-bottom:12px; display:none; align-items:center; gap:12px; flex-wrap:wrap;">
      <span id="selectionCount" style="color:#c9a0f8; font-size:0.85rem;">0 selected</span>
      <button class="btn btn-primary btn-small" onclick="rescanSelected()">Re-scan Selected</button>
      <button class="btn btn-secondary btn-small" onclick="clearSelection()">Clear</button>
    </div>

    <div id="demoList" class="demo-grid">
      <div class="empty-state">
        <div class="icon">--</div>
        <p>No demos in index yet. Run a scan or import URLs to get started.</p>
        <button class="btn btn-primary" onclick="switchTab('scan')">Run First Scan</button>
      </div>
    </div>

    <div style="margin-top:16px; display:flex; gap:8px; flex-wrap:wrap;">
      <button class="btn btn-download btn-small" onclick="downloadCSV()">Export CSV</button>
      <button class="btn btn-download btn-small" onclick="downloadJSON()">Export JSON</button>
    </div>
  </div>

  <!-- TAB: Query Engine -->
  <div class="tab-content" id="tab-query">
    <div class="panel">
      <h2>Ask the Demo Library</h2>
      <p style="font-size:0.82rem; color:#5a5468; margin-bottom:12px;">
        Natural language queries. The engine parses your intent, searches the index, and suggests deeper scans if needed.
      </p>
      <div class="search-bar" style="margin-bottom:0;">
        <input type="text" id="queryInput" placeholder="e.g. 'Which demos use social proof well?' or 'Find me an onboarding demo for enterprise'" onkeydown="if(event.key==='Enter')runQuery()">
        <button class="btn btn-primary" onclick="runQuery()">Ask</button>
      </div>
      <div style="font-size:0.72rem; color:#5a5468; margin-top:6px;">
        Examples: "demos with strong storytelling" | "which demos show customization?" | paste an email requesting a specific demo type
      </div>
    </div>

    <div id="queryResult" style="display:none;">
      <div class="panel" id="queryAnswer">
        <h2>Answer</h2>
        <div id="queryAnswerText" style="font-size:0.88rem; line-height:1.6; white-space:pre-wrap;"></div>
      </div>

      <div class="panel" id="queryScanSuggestion" style="display:none;">
        <h2>Scan Suggestion</h2>
        <div id="scanSuggestionText" style="font-size:0.85rem; color:#a8a0b8; margin-bottom:12px;"></div>
        <div id="scanLayerDetails"></div>
        <div style="margin-top:12px;">
          <button class="btn btn-primary btn-small" id="approveScanbtn" onclick="approveScan()">Run Scan</button>
          <span id="scanProgress" style="display:none; margin-left:8px;"><span class="spinner"></span> Scanning...</span>
        </div>
      </div>

      <div class="panel" id="queryCandidates">
        <h2>Matching Demos</h2>
        <div id="queryCandidatesList" class="demo-grid"></div>
      </div>
    </div>
  </div>

  <!-- TAB: Scan & Classify -->
  <div class="tab-content" id="tab-scan">
    <div class="panel">
      <h2>Run Pipeline</h2>
      <div class="form-row">
        <div class="form-group">
          <label>Demos to scan</label>
          <select id="limitSelect">
            <option value="0" selected>All new demos</option>
            <option value="5">First 5 new</option>
            <option value="10">First 10 new</option>
            <option value="20">First 20 new</option>
          </select>
        </div>
        <div class="form-group">
          <label>Classification</label>
          <select id="modeSelect">
            <option value="fast" selected>Fast (Haiku, text-only)</option>
            <option value="smart">Smart (Haiku + Sonnet for top demos)</option>
            <option value="full">Full (Sonnet + screenshots)</option>
            <option value="none">Skip classification</option>
          </select>
        </div>
        <div class="form-group">
          <label>Rescan?</label>
          <select id="rescanSelect">
            <option value="no" selected>No -- only new demos</option>
            <option value="all">Yes -- rescan all</option>
            <option value="query">Rescan by search...</option>
          </select>
        </div>
        <button class="btn btn-primary" id="startBtn" onclick="startRun()">Sync & Classify</button>
        <button class="btn btn-danger" id="stopBtn" onclick="stopRun()" style="display:none">Stop</button>
      </div>
      <div id="rescanQueryRow" style="display:none; margin-top:12px;">
        <input type="text" id="rescanQuery" placeholder="Search query to match demos for rescan..." style="width:100%;">
      </div>
      <div class="cost-estimate" id="costEstimate">
        <div class="est-item"><div class="est-val" id="estDemos">--</div><div class="est-lbl">Demos to scan</div></div>
        <div class="est-item"><div class="est-val" id="estCost">--</div><div class="est-lbl">Est. cost</div></div>
        <div class="est-item"><div class="est-val" id="estModel">--</div><div class="est-lbl">Model</div></div>
        <div class="est-item"><div class="est-val" id="estMode">--</div><div class="est-lbl">Screenshots</div></div>
      </div>
    </div>

    <div class="status-bar">
      <div class="status-dot" id="statusDot"></div>
      <span class="status-text" id="statusText">Ready</span>
    </div>

    <div class="log-panel" id="logPanel">
      <div class="line" style="color:#5a5468;">Pipeline logs will appear here...</div>
    </div>
  </div>

  <!-- TAB: Import -->
  <div class="tab-content" id="tab-import">
    <div class="panel">
      <h2>Bulk Import Demo URLs</h2>
      <p style="font-size:0.82rem; color:#5a5468; margin-bottom:12px;">
        Paste demo URLs below (one per line, or comma-separated). They'll be added to your index for future scanning.
      </p>
      <div class="import-area">
        <textarea id="importUrls" placeholder="https://app.storylane.io/demo/abc123&#10;https://app.storylane.io/demo/def456&#10;..."></textarea>
        <div class="form-row" style="margin-top:8px;">
          <div class="form-group" style="flex:1;">
            <label>Tags (optional, comma-separated)</label>
            <input type="text" id="importTags" placeholder="e.g. competitor, onboarding, enterprise">
          </div>
          <button class="btn btn-primary" onclick="bulkImport()">Import URLs</button>
        </div>
        <div class="import-result" id="importResult"></div>
      </div>
    </div>

    <div class="panel">
      <h2>Import from File</h2>
      <p style="font-size:0.82rem; color:#5a5468; margin-bottom:12px;">
        Use the CLI to import from a file:
      </p>
      <code style="background:#0a0714; padding:10px 14px; border-radius:8px; display:block; font-size:0.82rem; color:#8fd;">
        python3 run.py --import-file urls.txt
      </code>
    </div>
  </div>

  <!-- TAB: Tips & Tricks -->
  <div class="tab-content" id="tab-tips">
    <div class="panel">
      <h2>Best Practices Knowledge Base</h2>
      <p style="font-size:0.82rem; color:#5a5468; margin-bottom:12px;">
        Curated tips and tricks from the Demo Tips & Tricks library. These are automatically referenced during classification and query answering.
      </p>
      <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px;">
        <button class="btn btn-secondary btn-small" onclick="loadTips('')" style="font-size:0.78rem;">All</button>
        <span id="tipCategoryButtons"></span>
      </div>
      <div id="tipsList" style="font-size:0.85rem;"></div>
    </div>
    <div class="panel">
      <h2>Automated Suggestion Rules</h2>
      <p style="font-size:0.82rem; color:#5a5468; margin-bottom:12px;">
        These rules automatically generate improvement suggestions for every classified demo.
      </p>
      <div id="suggestionRules"></div>
    </div>
  </div>

  <!-- TAB: Settings -->
  <div class="tab-content" id="tab-settings">
    <div class="panel">
      <h2>API Key</h2>
      <div style="display:flex; gap:8px; align-items:center;">
        <input type="password" id="settingsApiKey" placeholder="sk-ant-..." style="flex:1;">
        <button class="btn btn-secondary" onclick="saveApiKeyFromSettings()">Update Key</button>
      </div>
    </div>

    <div class="panel">
      <h2>Share Tool</h2>
      <p style="font-size:0.82rem; color:#5a5468; margin-bottom:4px;">
        Export a shareable zip of the tool including your full index and knowledge base.
        Both options exclude the venv, API key, and generated reports (those are regeneratable).
      </p>
      <p style="font-size:0.8rem; color:#8a8494; margin-bottom:12px;">
        <strong style="color:#c8c0d8;">Without screenshots:</strong> Creates an empty screenshots folder.
        Place screenshots from Google Drive into it after unzipping.<br>
        <strong style="color:#c8c0d8;">With screenshots:</strong> Bundles everything — heavier but fully self-contained.
      </p>
      <div class="share-btn-row">
        <button class="btn btn-share" onclick="exportZip(false)">Export without Screenshots</button>
        <button class="btn btn-share" onclick="exportZip(true)">Export with Screenshots</button>
      </div>
      <div id="exportStatus" style="font-size:0.8rem; color:#8fd; margin-top:10px; display:none;"></div>
    </div>

    <div class="panel">
      <div class="collapsible-header" onclick="toggleSection('frameworkBody')">
        <h2>Custom Classification Framework</h2>
        <span class="toggle">Expand</span>
      </div>
      <div class="collapsible-body" id="frameworkBody">
        <p style="font-size:0.82rem; color:#5a5468; margin: 12px 0;">
          Paste a custom framework document to generate a rubric for classification.
        </p>
        <textarea id="frameworkText" placeholder="Paste your classification framework here..."></textarea>
        <div style="display:flex; gap:8px; margin-top:10px;">
          <button class="btn btn-secondary" onclick="generateRubric()">Generate Rubric</button>
          <span id="rubricSpinner" style="display:none"><span class="spinner"></span> Generating...</span>
          <button class="btn btn-secondary btn-small" onclick="viewCurrentRubric()" style="margin-left:auto">View Current</button>
          <button class="btn btn-secondary btn-small" onclick="resetRubric()">Reset to Default</button>
        </div>
        <div id="rubricPreviewSection" style="display:none; margin-top:12px;">
          <div class="rubric-preview" id="rubricPreview"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
let allDemos = [];
let currentFilter = 'all';
let selectMode = false;
let selectedKeys = new Set();
let searchTimeout = null;
let pollInterval = null;
let lastLogCount = 0;
let apiKeySet = false;

// ---- Init ----
document.addEventListener('DOMContentLoaded', () => {
  loadIndex();
  // Check if API key is already saved on the server — skip the modal if so
  fetch('/status').then(r=>r.json()).then(s => {
    if (s.api_key_set) {
      apiKeySet = true;
      document.getElementById('introModal').classList.add('hidden');
      updateApiBadge();
      // If a scan is already running, auto-switch to Scan tab and start polling
      if (s.running) {
        switchTab('scan');
        startPolling();
      }
    }
  });
  document.getElementById('rescanSelect').addEventListener('change', (e) => {
    document.getElementById('rescanQueryRow').style.display = e.target.value === 'query' ? 'block' : 'none';
    updateCostEstimate();
  });
  document.getElementById('limitSelect').addEventListener('change', updateCostEstimate);
  document.getElementById('modeSelect').addEventListener('change', updateCostEstimate);
});

// ---- Tabs ----
const tabOrder = ['browse','query','scan','import','tips','settings'];
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelector(`.tab-content#tab-${name}`).classList.add('active');
  document.querySelectorAll('.tab')[tabOrder.indexOf(name)].classList.add('active');
  if (name === 'browse') loadIndex();
  if (name === 'tips') loadTips('');
}

// ---- Index loading ----
function loadIndex() {
  fetch('/index').then(r=>r.json()).then(data => {
    allDemos = data.demos || [];
    const s = data.stats || {};
    document.getElementById('statTotal').textContent = s.total_demos || 0;
    document.getElementById('statLight').textContent = s.light_scan || 0;
    document.getElementById('statFull').textContent = s.full_scan || 0;
    document.getElementById('statUnscanned').textContent = s.unscanned || 0;
    document.getElementById('statAvg').textContent = s.avg_score ? s.avg_score + '/10' : '--';
    document.getElementById('statTop').textContent = s.highest_score ? s.highest_score + '/10' : '--';
    updateCostEstimate();
    buildFilterBar(s.type_breakdown || {});
    renderDemos(allDemos);
  }).catch(()=>{});
}

function buildFilterBar(types) {
  const bar = document.getElementById('filterBar');
  const missingCount = allDemos.filter(d => d.screenshots_captured && !d.screenshots_present).length;
  let html = '<span class="filter-chip active" onclick="setFilter(\\'all\\', this)">All<span class="count-badge">' + allDemos.length + '</span></span>';
  html += '<span class="filter-chip" onclick="setFilter(\\'unscanned\\', this)">Unscanned<span class="count-badge">' + allDemos.filter(d=>!d.last_scanned_at).length + '</span></span>';
  if (missingCount > 0) {
    html += '<span class="filter-chip" onclick="setFilter(\\'missing_screenshots\\', this)" title="Previously scanned with screenshots but files are missing on this machine">Screenshots Missing<span class="count-badge">' + missingCount + '</span></span>';
  }
  for (const [type, count] of Object.entries(types)) {
    if (type === 'unclassified') continue;
    const short = type.length > 30 ? type.substring(0, 27) + '...' : type;
    html += '<span class="filter-chip" onclick="setFilter(\\'' + esc(type) + '\\', this)">' + esc(short) + '<span class="count-badge">' + count + '</span></span>';
  }
  bar.innerHTML = html;
}

function setFilter(filter, el) {
  currentFilter = filter;
  document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
  if (el) el.classList.add('active');
  applyFilters();
}

function applyFilters() {
  const query = document.getElementById('searchInput').value.trim().toLowerCase();
  let filtered = allDemos;

  if (currentFilter === 'unscanned') {
    filtered = filtered.filter(d => !d.last_scanned_at);
  } else if (currentFilter === 'missing_screenshots') {
    filtered = filtered.filter(d => d.screenshots_captured && !d.screenshots_present);
  } else if (currentFilter !== 'all') {
    filtered = filtered.filter(d => (d.classification?.type || '').toLowerCase().includes(currentFilter.toLowerCase()));
  }

  if (query) {
    filtered = filtered.filter(d => {
      const searchable = [d.name, d.category, d.classification?.type, d.classification?.summary, ...(d.tags||[]), d.demo_url, d.showcase_url].join(' ').toLowerCase();
      return query.split(' ').every(w => searchable.includes(w));
    });
  }

  renderDemos(filtered);
}

function toggleSelectMode() {
  selectMode = !selectMode;
  if (!selectMode) selectedKeys.clear();
  const btn = document.getElementById('selectModeBtn');
  btn.textContent = selectMode ? 'Cancel' : 'Select';
  btn.style.background = selectMode ? '#3b1f5e' : '';
  updateSelectionBar();
  applyFilters();
}

function toggleSelect(key, checked) {
  if (checked) selectedKeys.add(key);
  else selectedKeys.delete(key);
  updateSelectionBar();
  applyFilters();
}

function clearSelection() {
  selectedKeys.clear();
  updateSelectionBar();
  applyFilters();
}

function updateSelectionBar() {
  const bar = document.getElementById('selectionBar');
  const count = document.getElementById('selectionCount');
  if (selectMode && selectedKeys.size > 0) {
    bar.style.display = 'flex';
    count.textContent = selectedKeys.size + ' selected';
  } else {
    bar.style.display = 'none';
  }
}

function rescanDemo(key, name) {
  if (!confirm('Re-scan "' + name + '"? This will re-walk the demo and update its classification.')) return;
  launchRescan([key]);
}

function rescanSelected() {
  if (selectedKeys.size === 0) return;
  if (!confirm('Re-scan ' + selectedKeys.size + ' selected demo(s)? This will re-walk and re-classify them.')) return;
  launchRescan(Array.from(selectedKeys));
}

function launchRescan(keys) {
  const modeEl = document.getElementById('modeSelect');
  const mode = modeEl ? modeEl.value : 'fast';
  fetch('/run-scan', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ rescan_keys: keys.join(','), mode })
  }).then(r => r.json()).then(d => {
    if (d.error) { alert('Error: ' + d.error); return; }
    if (selectMode) toggleSelectMode();
    switchTab('scan');
    startPolling();
  }).catch(e => alert('Failed to start rescan: ' + e));
}

function debounceSearch() {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(applyFilters, 200);
}

function smartQuery() {
  const query = document.getElementById('searchInput').value.trim();
  if (!query) return;
  if (!apiKeySet) { alert('Smart search requires an API key. Set one in Settings.'); return; }

  document.getElementById('demoList').innerHTML = '<div class="empty-state"><span class="spinner"></span><p style="margin-top:12px;">Searching with AI...</p></div>';

  fetch('/query?q=' + encodeURIComponent(query))
    .then(r => r.json())
    .then(data => {
      if (data.demos && data.demos.length) {
        renderDemos(data.demos);
      } else {
        document.getElementById('demoList').innerHTML = '<div class="empty-state"><p>No demos found for that query. Try different terms.</p></div>';
      }
    })
    .catch(() => {
      document.getElementById('demoList').innerHTML = '<div class="empty-state"><p>Smart search failed. Try keyword search instead.</p></div>';
    });
}

function renderDemos(demos) {
  const el = document.getElementById('demoList');
  if (!demos.length) {
    el.innerHTML = '<div class="empty-state"><p>No demos match your filters.</p></div>';
    return;
  }

  el.innerHTML = demos.map(d => {
    const cls = d.classification || {};
    const badge = getBadge(cls.type, d.last_scanned_at);
    const score = cls.overall_score || cls.score;

    // Scan depth badge
    let depthBadge = '';
    if (d.last_scanned_at) {
      if (d.screenshots_captured) {
        depthBadge = '<span class="badge badge-depth-full">Full</span> ';
      } else {
        depthBadge = '<span class="badge badge-depth-light">Light</span> ';
      }
    }

    const missingPhotoBadge = (d.screenshots_captured && !d.screenshots_present)
      ? '<span class="badge badge-photo-missing" title="Screenshots missing on this machine">No Screenshots</span> '
      : '';

    const chaptersBadge = d.has_chapters
      ? '<span class="badge badge-chapters" title="' + (d.chapter_count > 0 ? d.chapter_count + ' chapters' : 'Chapter-based demo') + '">' + (d.chapter_count > 0 ? d.chapter_count + ' Chapters' : 'Chapters') + '</span> '
      : '';

    const scanDate = d.last_scanned_at ? d.last_scanned_at.substring(0,10) : null;
    const metaParts = [];
    if (scanDate) metaParts.push(scanDate);
    if (d.steps_captured) metaParts.push(d.steps_captured + ' steps');
    if (d.source && d.source !== 'showcase') metaParts.push(d.source);

    // Summary truncated to ~120 chars
    const summary = cls.summary ? (cls.summary.length > 120 ? cls.summary.substring(0,117) + '...' : cls.summary) : '';

    // Top 3 tags only
    let tags = '';
    if (d.tags && d.tags.length) {
      tags = '<div class="tag-list">' + d.tags.slice(0,3).map(t =>
        '<span class="tag">' + esc(t) + '<span class="x" onclick="removeTag(\\'' + esc(d.key) + '\\',\\'' + esc(t) + '\\')">x</span></span>'
      ).join('') + (d.tags.length > 3 ? '<span class="tag" style="color:#5a5468;">+' + (d.tags.length-3) + '</span>' : '') + '</div>';
    }

    const cardCls = 'demo-card' + (selectMode && selectedKeys.has(d.key) ? ' selected' : '');
    return '<div class="' + cardCls + '"' + (selectMode ? ' onclick="toggleSelect(\\'' + esc(d.key) + '\\', !selectedKeys.has(\\'' + esc(d.key) + '\\'))" style="cursor:pointer;"' : '') + '>'
      + '<div class="tile-top">'
      + '<div class="name">' + esc(d.display_name || d.name) + '</div>'
      + (score ? '<div class="tile-score">' + score + '<span style="font-size:0.65rem;color:#8a8494;">/10</span></div>' : '')
      + '</div>'
      + '<div style="display:flex;gap:4px;flex-wrap:wrap;align-items:center;">'
      + depthBadge + chaptersBadge + missingPhotoBadge + badge
      + '</div>'
      + (metaParts.length ? '<div class="meta">' + esc(metaParts.join(' · ')) + '</div>' : '<div class="meta" style="color:#3a3448;">Not yet scanned</div>')
      + (summary ? '<div class="summary-text" style="-webkit-line-clamp:3;display:-webkit-box;-webkit-box-orient:vertical;overflow:hidden;">' + esc(summary) + '</div>' : '')
      + tags
      + '<div class="actions" style="margin-top:auto;">'
      + (d.demo_url ? '<a href="' + esc(d.demo_url) + '" target="_blank" class="btn btn-secondary btn-small">Open</a>' : '')
      + '<button class="btn btn-secondary btn-small" onclick="promptTag(\\'' + esc(d.key) + '\\')">+ Tag</button>'
      + '<button class="btn btn-secondary btn-small" onclick="rescanDemo(\\'' + esc(d.key) + '\\',\\'' + esc(d.display_name||d.name) + '\\')" title="Re-walk and re-classify this demo">Re-scan</button>'
      + '</div>'
      + (selectMode ? '<input type="checkbox" class="tile-select-cb" ' + (selectedKeys.has(d.key) ? 'checked' : '') + ' onchange="toggleSelect(\\'' + esc(d.key) + '\\', this.checked)" onclick="event.stopPropagation()">' : '')
      + '</div>';
  }).join('');
}

function getBadge(type, scanned) {
  if (!scanned) return '<span class="badge badge-unscanned">Unscanned</span>';
  if (!type) return '<span class="badge badge-unscanned">Unclassified</span>';
  const t = type.toLowerCase();
  let cls = 'badge-generic';
  if (t.includes('strong') || t.includes('good')) cls = 'badge-strong';
  else if (t.includes('dump') || t.includes('walkthrough')) cls = 'badge-weak';
  else if (t.includes('click')) cls = 'badge-click';
  else if (t.includes('gated') || t.includes('inaccessible')) cls = 'badge-gated';
  return '<span class="badge ' + cls + '">' + esc(type) + '</span>';
}

// ---- Tags ----
function promptTag(key) {
  const tag = prompt('Enter tag(s) to add (comma-separated):');
  if (!tag) return;
  const tags = tag.split(',').map(t => t.trim()).filter(Boolean);
  fetch('/tag', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ key, tags })
  }).then(r=>r.json()).then(() => loadIndex());
}

function removeTag(key, tag) {
  fetch('/remove-tag', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ key, tag })
  }).then(r=>r.json()).then(() => loadIndex());
}

// ---- Import ----
function bulkImport() {
  const urls = document.getElementById('importUrls').value.trim();
  const tagsStr = document.getElementById('importTags').value.trim();
  const tags = tagsStr ? tagsStr.split(',').map(t=>t.trim()).filter(Boolean) : [];

  if (!urls) { alert('Paste some URLs first.'); return; }

  fetch('/import-urls', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ urls, tags })
  }).then(r=>r.json()).then(data => {
    if (data.error) {
      document.getElementById('importResult').innerHTML = '<span style="color:#f88">' + data.error + '</span>';
      return;
    }
    document.getElementById('importResult').innerHTML =
      'Imported ' + data.added + ' new demos (' + data.already_existed + ' already existed). Index now has ' + data.index_size + ' demos.';
    document.getElementById('importUrls').value = '';
    document.getElementById('importTags').value = '';
    loadIndex();
  });
}

// ---- Scan pipeline ----
// ---- Cost Estimate ----
function updateCostEstimate() {
  const limitSel = document.getElementById('limitSelect');
  const modeSel = document.getElementById('modeSelect');
  const rescanSel = document.getElementById('rescanSelect');
  if (!limitSel || !modeSel) return;

  const mode = modeSel.value;
  const rescan = rescanSel ? rescanSel.value : 'no';
  const limitVal = parseInt(limitSel.value) || 0;

  const total = parseInt(document.getElementById('statTotal')?.textContent) || 0;
  const unscanned = parseInt(document.getElementById('statUnscanned')?.textContent) || 0;

  let count;
  if (rescan === 'all') count = total;
  else count = unscanned;
  if (limitVal > 0) count = Math.min(count, limitVal);

  let costPerDemo, modelLabel, screenshotsLabel;
  if (mode === 'fast') {
    costPerDemo = 0.001; modelLabel = 'Haiku'; screenshotsLabel = 'No';
  } else if (mode === 'full') {
    costPerDemo = 0.15; modelLabel = 'Sonnet'; screenshotsLabel = 'Yes';
  } else if (mode === 'smart') {
    costPerDemo = 0.035; modelLabel = 'Haiku + Sonnet'; screenshotsLabel = 'Top demos';
  } else {
    costPerDemo = 0; modelLabel = '--'; screenshotsLabel = '--';
  }

  const est = (count * costPerDemo).toFixed(3);
  document.getElementById('estDemos').textContent = count;
  document.getElementById('estCost').textContent = mode === 'none' ? '$0' : '~$' + est;
  document.getElementById('estModel').textContent = modelLabel;
  document.getElementById('estMode').textContent = screenshotsLabel;
}

let pendingScanParams = null;

function startRun() {
  const limit = parseInt(document.getElementById('limitSelect').value);
  const mode = document.getElementById('modeSelect').value;
  const noClassify = mode === 'none';
  const rescanSel = document.getElementById('rescanSelect').value;
  const rescan = rescanSel !== 'no';
  const rescanQuery = rescanSel === 'query' ? document.getElementById('rescanQuery').value.trim() : '';

  if (!noClassify && !apiKeySet) { showApiKeyModal(); return; }

  pendingScanParams = { limit: limit || 0, no_classify: noClassify, mode: noClassify ? 'fast' : mode, rescan, rescan_query: rescanQuery };

  // For full/smart mode, check for missing screenshots first
  if (!noClassify && (mode === 'full' || mode === 'smart')) {
    fetch('/screenshot-status').then(r=>r.json()).then(data => {
      if (data.count > 0) {
        showScreenshotModal(data.missing);
      } else {
        launchScan(pendingScanParams);
      }
    }).catch(() => launchScan(pendingScanParams));
    return;
  }

  launchScan(pendingScanParams);
}

function showScreenshotModal(missing) {
  document.getElementById('screenshotModalDesc').textContent =
    missing.length + ' demo(s) were previously scanned with screenshots, but the screenshot files are missing on this machine.';
  document.getElementById('screenshotMissingList').innerHTML =
    missing.map(d => '• ' + esc(d.name)).join('<br>');
  document.getElementById('screenshotModal').classList.remove('hidden');
}

function proceedTextOnly() {
  document.getElementById('screenshotModal').classList.add('hidden');
  launchScan(pendingScanParams);
}

function retrieveScreenshotsThenScan() {
  document.getElementById('screenshotModal').classList.add('hidden');
  // Get keys from the missing list
  fetch('/screenshot-status').then(r=>r.json()).then(data => {
    const keys = data.missing.map(d => d.key);
    switchTab('scan');
    document.getElementById('startBtn').style.display = 'none';
    document.getElementById('stopBtn').style.display = 'inline-block';
    document.getElementById('logPanel').innerHTML = '';
    document.getElementById('statusDot').className = 'status-dot running';
    document.getElementById('statusText').textContent = 'Retrieving screenshots...';
    lastLogCount = 0;
    fetch('/retrieve-screenshots', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ demo_keys: keys })
    }).then(r=>r.json()).then(resp => {
      if (resp.error) {
        document.getElementById('statusText').textContent = 'Error: ' + resp.error;
        document.getElementById('startBtn').style.display = 'inline-block';
        document.getElementById('stopBtn').style.display = 'none';
        return;
      }
      startPolling();
      // After retrieval completes, launch the scan
      const waitForRetrieval = setInterval(() => {
        fetch('/status').then(r=>r.json()).then(s => {
          if (s.finished && !s.running) {
            clearInterval(waitForRetrieval);
            launchScan(pendingScanParams);
          }
        });
      }, 2000);
    });
  });
}

function launchScan(params) {
  document.getElementById('startBtn').style.display = 'none';
  document.getElementById('stopBtn').style.display = 'inline-block';
  document.getElementById('logPanel').innerHTML = '';
  document.getElementById('statusDot').className = 'status-dot running';
  document.getElementById('statusText').textContent = 'Starting...';
  lastLogCount = 0;

  fetch('/start', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(params)
  }).then(r=>r.json()).then(data => {
    if (data.error) {
      document.getElementById('statusDot').className = 'status-dot error';
      document.getElementById('statusText').textContent = data.error;
      document.getElementById('startBtn').style.display = 'inline-block';
      document.getElementById('stopBtn').style.display = 'none';
      if (data.error.includes('API key')) showApiKeyModal();
      return;
    }
    startPolling();
  });
}

function stopRun() {
  if (!confirm('Stop the current scan?\\n\\nAll demos scanned so far are saved — you won\\'t lose progress. You can resume later to scan the rest.')) return;
  fetch('/stop', { method: 'POST' });
  document.getElementById('statusDot').className = 'status-dot error';
  document.getElementById('statusText').textContent = 'Stopped (partial results saved)';
  document.getElementById('startBtn').style.display = 'inline-block';
  document.getElementById('stopBtn').style.display = 'none';
  if (pollInterval) clearInterval(pollInterval);
  loadIndex();
}

function startPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(pollStatus, 1500);
  pollStatus();
}

function pollStatus() {
  fetch('/status').then(r=>r.json()).then(data => {
    if (data.log_count > lastLogCount) {
      const panel = document.getElementById('logPanel');
      if (panel) {
        const newLines = data.log.slice(lastLogCount);
        for (const line of newLines) {
          const div = document.createElement('div');
          div.className = 'line';
          div.textContent = line;
          panel.appendChild(div);
        }
        panel.scrollTop = panel.scrollHeight;
      }
      lastLogCount = data.log_count;
    }

    // Progress summary
    const prog = data.progress || {};
    const bar = document.getElementById('progressBar');
    if (data.running) {
      let txt = 'Running';
      if (prog.classified > 0) txt += ' · ' + prog.classified + ' classified';
      if (prog.layer) txt += ' · layer scan ' + prog.layer.current + '/' + prog.layer.total;
      if (prog.estimated_cost > 0) txt += ' · ~$' + prog.estimated_cost.toFixed(3) + ' spent';
      document.getElementById('statusText').textContent = txt;
      // Show a header pill
      if (bar) {
        bar.style.display = 'inline-flex';
        bar.textContent = txt;
      }
    } else if (bar) {
      bar.style.display = 'none';
    }

    if (data.finished && !data.running) {
      clearInterval(pollInterval);
      document.getElementById('startBtn').style.display = 'inline-block';
      document.getElementById('stopBtn').style.display = 'none';
      document.getElementById('statusDot').className = data.error ? 'status-dot error' : 'status-dot done';
      let doneTxt = data.error ? 'Error: ' + data.error : 'Done!';
      if (prog.classified > 0) doneTxt += ' (' + prog.classified + ' classified, ~$' + (prog.estimated_cost || 0).toFixed(3) + ')';
      document.getElementById('statusText').textContent = doneTxt;
      loadIndex();
    }
  }).catch(()=>{});
}

// ---- API Key ----
function showApiKeyModal() {
  document.getElementById('introModal').classList.remove('hidden');
  document.getElementById('apiKeyInput').focus();
}
function saveApiKey() {
  const key = document.getElementById('apiKeyInput').value.trim();
  if (!key) { showApiError('Please enter your API key.'); return; }
  if (!key.startsWith('sk-ant-')) { showApiError('Key should start with "sk-ant-".'); return; }
  fetch('/save-api-key', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ api_key: key })
  }).then(r=>r.json()).then(data => {
    if (data.status === 'saved') {
      apiKeySet = true;
      document.getElementById('introModal').classList.add('hidden');
      updateApiBadge();
    }
  });
}
function skipApiKey() {
  document.getElementById('introModal').classList.add('hidden');
  document.getElementById('modeSelect').value = 'none';
}
function showApiError(msg) {
  const el = document.getElementById('apiKeyError');
  el.textContent = msg; el.style.display = 'block';
}
function updateApiBadge() {
  const b = document.getElementById('apiBadge');
  b.className = apiKeySet ? 'api-badge set' : 'api-badge unset';
  b.textContent = apiKeySet ? 'API Key Set' : 'No API Key';
}
function saveApiKeyFromSettings() {
  const key = document.getElementById('settingsApiKey').value.trim();
  if (!key) return;
  fetch('/save-api-key', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ api_key: key })
  }).then(r=>r.json()).then(() => { apiKeySet = true; updateApiBadge(); });
}

// ---- Downloads ----
function downloadCSV() { window.location.href = '/download-csv'; }
function downloadJSON() { window.location.href = '/download-json'; }

function exportZip(withScreenshots) {
  const status = document.getElementById('exportStatus');
  status.style.display = 'block';
  status.textContent = withScreenshots ? 'Preparing zip with screenshots (may take a moment)...' : 'Preparing zip...';
  const url = '/export-zip?screenshots=' + (withScreenshots ? '1' : '0');
  fetch(url).then(r => {
    if (!r.ok) { status.textContent = 'Export failed.'; return; }
    const fname = withScreenshots ? 'storylane-demo-classifier-with-screenshots.zip' : 'storylane-demo-classifier.zip';
    return r.blob().then(blob => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = fname;
      a.click();
      URL.revokeObjectURL(a.href);
      status.textContent = 'Downloaded ' + fname;
      setTimeout(() => { status.style.display = 'none'; }, 4000);
    });
  }).catch(() => { status.textContent = 'Export failed.'; });
}

// ---- Settings: Rubric ----
function toggleSection(id) {
  const el = document.getElementById(id);
  el.classList.toggle('open');
}
function generateRubric() {
  const docText = document.getElementById('frameworkText').value.trim();
  if (!docText) { alert('Paste your framework document first.'); return; }
  document.getElementById('rubricSpinner').style.display = 'inline-flex';
  document.getElementById('rubricPreviewSection').style.display = 'none';
  fetch('/upload-framework', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({doc_text:docText}) });
  const poll = setInterval(() => {
    fetch('/rubric-status').then(r=>r.json()).then(data => {
      if (data.status === 'ready') {
        clearInterval(poll);
        document.getElementById('rubricSpinner').style.display = 'none';
        document.getElementById('rubricPreview').textContent = data.rubric;
        document.getElementById('rubricPreviewSection').style.display = 'block';
      } else if (data.status === 'error') {
        clearInterval(poll);
        document.getElementById('rubricSpinner').style.display = 'none';
        alert('Error: ' + (data.error || 'Unknown'));
      }
    });
  }, 2000);
}
function viewCurrentRubric() {
  fetch('/rubric-status').then(r=>r.json()).then(data => {
    if (data.rubric) {
      document.getElementById('rubricPreview').textContent = data.rubric;
      document.getElementById('rubricPreviewSection').style.display = 'block';
    } else {
      fetch('/default-rubric').then(r=>r.json()).then(d => {
        document.getElementById('rubricPreview').textContent = d.rubric;
        document.getElementById('rubricPreviewSection').style.display = 'block';
      });
    }
  });
}
function resetRubric() {
  fetch('/reset-rubric', { method:'POST' });
  document.getElementById('rubricPreviewSection').style.display = 'none';
}

// ---- Query Engine ----
let lastQueryPlan = null;
let lastQueryText = '';

function runQuery() {
  const query = document.getElementById('queryInput').value.trim();
  if (!query) return;
  if (!apiKeySet) { alert('Queries require an API key.'); return; }

  document.getElementById('queryResult').style.display = 'block';
  document.getElementById('queryAnswer').innerHTML = '<h2>Answer</h2><div style="text-align:center;padding:20px;"><span class="spinner"></span> Analyzing query...</div>';
  document.getElementById('queryScanSuggestion').style.display = 'none';
  document.getElementById('queryCandidatesList').innerHTML = '';

  fetch('/query-engine', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ query })
  }).then(r=>r.json()).then(data => {
    if (data.error) {
      document.getElementById('queryAnswer').innerHTML = '<h2>Answer</h2><div style="color:#f88;">' + esc(data.error) + '</div>';
      return;
    }

    lastQueryPlan = data.plan;
    lastQueryText = query;

    // Show answer with markdown link rendering
    let answerHtml = '<h2>Answer</h2><div id="queryAnswerText" style="font-size:0.88rem; line-height:1.6; white-space:pre-wrap;">' + renderMarkdownLinks(data.answer) + '</div>';

    // Show learning indicator
    if (data.themes && data.themes.length > 0) {
      answerHtml += '<div style="margin-top:12px; padding:8px 12px; background:rgba(100,200,150,0.1); border-left:3px solid #6c8; border-radius:4px; font-size:0.78rem; color:#8fa;">';
      answerHtml += 'Learned: ' + data.themes.map(t => '<span style="background:rgba(100,200,150,0.2); padding:2px 8px; border-radius:10px; margin:0 3px;">' + esc(t) + '</span>').join('');
      if (data.knowledge_status) {
        answerHtml += ' <span style="color:#5a5468; margin-left:8px;">(' + data.knowledge_status.total_queries_learned + ' queries learned, ' + data.knowledge_status.themes_known.length + ' themes indexed)</span>';
      }
      answerHtml += '</div>';
    }
    document.getElementById('queryAnswer').innerHTML = answerHtml;

    // Show scan suggestion if needed
    const plan = data.plan;
    if (plan.need_layer_scan > 0 && plan.required_layers && plan.required_layers.length > 0) {
      document.getElementById('queryScanSuggestion').style.display = 'block';
      let html = '<p>' + plan.need_layer_scan + ' demos could be scanned for deeper data. Estimated cost: ~$' + plan.total_estimated_cost.toFixed(3) + '</p>';
      html += '<div style="margin-top:8px;">';
      for (const [layer, cost] of Object.entries(plan.layer_costs)) {
        html += '<div style="font-size:0.82rem; color:#8a8494; margin:4px 0;">Layer: <strong>' + esc(layer) + '</strong> -- ' + cost.to_scan + ' demos @ $' + cost.cost_per_demo + '/demo</div>';
      }
      html += '</div>';
      document.getElementById('scanSuggestionText').innerHTML = html;
    }

    // Show candidates
    if (data.candidates && data.candidates.length) {
      const listEl = document.getElementById('queryCandidatesList');
      listEl.innerHTML = data.candidates.map(d => {
        const cls = d.classification || {};
        const badge = getBadge(cls.type, true);
        const score = cls.overall_score || cls.score;
        let layers = d.layers && d.layers.length ? '<div style="font-size:0.72rem; color:#5a5468; margin-top:4px;">Layers: ' + d.layers.join(', ') + '</div>' : '';
        return '<div class="demo-card"><div class="header"><div class="name">' + esc(d.display_name || d.name) + '</div><div>' + badge + (score ? ' <span style="font-weight:600;">' + score + '/10</span>' : '') + '</div></div>'
          + (cls.summary ? '<div class="summary-text">' + esc(cls.summary) + '</div>' : '')
          + layers
          + (d.demo_url ? '<div class="actions"><a href="' + esc(d.demo_url) + '" target="_blank" class="btn btn-secondary btn-small">Open Demo</a></div>' : '')
          + '</div>';
      }).join('');
    }
  }).catch(e => {
    document.getElementById('queryAnswer').innerHTML = '<h2>Answer</h2><div style="color:#f88;">Query failed: ' + e + '</div>';
  });
}

function approveScan() {
  if (!lastQueryPlan || !lastQueryPlan.required_layers) return;

  document.getElementById('approveScanbtn').style.display = 'none';
  document.getElementById('scanProgress').style.display = 'inline-flex';

  fetch('/run-scan', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      layers: lastQueryPlan.required_layers,
      limit: Math.min(lastQueryPlan.need_layer_scan, 20),
      query_context: lastQueryText || null
    })
  }).then(r=>r.json()).then(data => {
    if (data.error) {
      alert('Scan error: ' + data.error);
      document.getElementById('approveScanbtn').style.display = 'inline-block';
      document.getElementById('scanProgress').style.display = 'none';
      return;
    }
    // Poll for completion
    const poll = setInterval(() => {
      fetch('/status').then(r=>r.json()).then(s => {
        if (s.finished) {
          clearInterval(poll);
          document.getElementById('scanProgress').innerHTML = '<span style="color:#8fd;">Done! Re-run your query for updated results.</span>';
        }
      });
    }, 2000);
  });
}

// ---- Inline Suggestions on Demo Cards ----
function renderSuggestionsInline(d) {
  const sug = d.auto_suggestions || [];
  const tips = d.relevant_tips || [];
  if (!sug.length && !tips.length) return '';

  let html = '<div style="margin-top:8px; padding:8px; background:#1a1528; border-radius:6px; font-size:0.8rem;">';

  if (sug.length) {
    html += '<div style="color:#fa0; font-weight:600; margin-bottom:4px;">Suggestions</div>';
    html += sug.map(s => {
      const sev = s.severity === 'high' ? '#f66' : s.severity === 'medium' ? '#fa0' : '#8a8494';
      return '<div style="margin-bottom:3px; padding-left:8px; border-left:2px solid ' + sev + ';">'
        + '<span style="color:' + sev + '; font-size:0.72rem; text-transform:uppercase;">' + esc(s.category) + '</span> '
        + esc(s.suggestion) + '</div>';
    }).join('');
  }

  if (tips.length) {
    html += '<div style="color:#6af; font-weight:600; margin-top:6px; margin-bottom:4px;">Relevant Tips</div>';
    html += tips.slice(0,3).map(t => {
      const link = t.demo_link ? ' <a href="' + esc(t.demo_link) + '" target="_blank" style="color:#fa0; font-size:0.72rem;">example</a>' : '';
      const co = t.company ? ' (' + esc(t.company) + ')' : '';
      return '<div style="margin-bottom:3px; padding-left:8px; border-left:2px solid #6af;">'
        + esc(t.tip) + co + link + '</div>';
    }).join('');
  }

  html += '</div>';
  return html;
}

// ---- Tips & Tricks ----
let tipsData = null;

function loadTips(category) {
  const url = category ? '/tips?category=' + encodeURIComponent(category) : '/tips';
  fetch(url).then(r=>r.json()).then(data => {
    tipsData = data;
    renderTipCategories(data.categories || []);
    renderTips(data.tips || []);
    renderSuggestionRules(data.suggestions_rules || []);
  });
}

function renderTipCategories(cats) {
  const el = document.getElementById('tipCategoryButtons');
  if (!el) return;
  el.innerHTML = cats.map(c =>
    '<button class="btn btn-secondary btn-small" onclick="loadTips(\\'' + esc(c) + '\\')" style="font-size:0.78rem;">' + esc(c) + '</button>'
  ).join('');
}

function renderTips(tips) {
  const el = document.getElementById('tipsList');
  if (!el) return;
  if (!tips.length) { el.innerHTML = '<div class="empty-state"><p>No tips found.</p></div>'; return; }

  el.innerHTML = tips.map(t => {
    const link = t.demo_link ? '<a href="' + esc(t.demo_link) + '" target="_blank" style="color:#fa0; text-decoration:none; font-size:0.78rem;">View example</a>' : '';
    const company = t.company ? '<span style="color:#5a5468;"> (' + esc(t.company) + ')</span>' : '';
    return '<div style="padding:10px 0; border-bottom:1px solid #1a1528;">'
      + '<div style="display:flex; justify-content:space-between; align-items:start;">'
      + '<div><span class="badge badge-generic" style="font-size:0.7rem; margin-right:6px;">' + esc(t.category) + '</span>'
      + '<strong>' + esc(t.tip) + '</strong>' + company + '</div>'
      + '<div>' + link + '</div></div>'
      + (t.reasoning ? '<div style="color:#8a8494; font-size:0.8rem; margin-top:4px;">' + esc(t.reasoning) + '</div>' : '')
      + (t.outcome ? '<div style="color:#6a6; font-size:0.78rem; margin-top:2px;">Outcome: ' + esc(t.outcome) + '</div>' : '')
      + '</div>';
  }).join('');
}

function renderSuggestionRules(rules) {
  const el = document.getElementById('suggestionRules');
  if (!el) return;
  if (!rules.length) { el.innerHTML = '<p style="color:#5a5468;">No rules loaded.</p>'; return; }

  el.innerHTML = '<table style="width:100%; font-size:0.82rem; border-collapse:collapse;">'
    + '<tr style="border-bottom:1px solid #2a2438; color:#8a8494;"><th style="text-align:left; padding:6px;">Category</th><th style="text-align:left; padding:6px;">Trigger</th><th style="text-align:left; padding:6px;">Suggestion</th></tr>'
    + rules.map(r =>
      '<tr style="border-bottom:1px solid #1a1528;">'
      + '<td style="padding:6px; color:#fa0; white-space:nowrap;">' + esc(r.category) + '</td>'
      + '<td style="padding:6px; color:#a8a0b8;">' + esc(r.logic) + '</td>'
      + '<td style="padding:6px;">' + esc(r.suggestion) + '</td></tr>'
    ).join('') + '</table>';
}

// ---- Util ----
function esc(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// Render a subset of markdown: [text](url) links, **bold**, and bare URLs.
// Escapes everything else so user-provided text can't inject HTML.
function renderMarkdownLinks(s) {
  if (!s) return '';
  let html = esc(s);
  // [text](url) -> anchor
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_m, text, url) =>
    '<a href="' + url + '" target="_blank" style="color:#fa0; text-decoration:underline;">' + text + '</a>'
  );
  // Bare URLs -> anchor (only if not already inside an anchor)
  html = html.replace(/(^|[\s(])(https?:\/\/[^\s)<]+)/g, (_m, pre, url) =>
    pre + '<a href="' + url + '" target="_blank" style="color:#fa0; text-decoration:underline;">' + url + '</a>'
  );
  // **bold**
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  return html;
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    # Load saved key from disk first, then prefer env var if set
    saved_key = _load_saved_api_key()
    if saved_key:
        state["api_key"] = saved_key
        print(f"   Loaded saved API key (last 4: ...{saved_key[-4:]})")
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        state["api_key"] = env_key

    print(f"Storylane Demo Classifier")
    print(f"   Open http://localhost:{PORT} in your browser")
    print(f"   Press Ctrl+C to stop the server")
    print()

    import webbrowser
    webbrowser.open(f"http://localhost:{PORT}")

    server = HTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")
        server.server_close()
