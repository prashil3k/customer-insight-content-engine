# Storylane Content Engine

End-to-end content creation pipeline — from customer call insights to publication-ready articles with SEO and QC checks built in.

## Start the server

```bash
cd "/Users/prashil3k/Documents/Claude Code/storylane-content-engine"
python3 app.py
```

Then open **http://localhost:8001** in your browser.

> API keys are set from the UI — go to **Settings → API Keys** and paste them in. They persist to `data/settings.json` and take effect immediately without a restart.

## First-time setup

1. **Settings → API Keys** — add your Anthropic and Ahrefs keys
2. **Settings → Company Intelligence** — paste Storylane URLs and hit *Run Intelligence Scan*
3. **Insight Feed → Upload Transcript** or drop files into `watch/grain/` or `watch/sybill/`
4. **Topic Pipeline → Generate Topics** — the system proposes articles based on your insights
5. Click any topic card to open it in the **Article Editor** and walk through the pipeline

## Pipeline stages

```
Idea → Keywords → Draft → QC → SEO → Done
```

Each stage has a dedicated screen in the Article Editor. QC and SEO checks run in the right panel with per-suggestion accept/reject controls and an Apply All button.

## Watch folders

Drop transcript exports here and the scheduler picks them up automatically:

```
watch/grain/    ← Grain .txt / .vtt exports
watch/sybill/   ← Sybill .txt exports
```

Or trigger a manual scan from **Settings → Run Insight Scan Now**.

## Dependencies

```bash
pip3 install -r requirements.txt
```

## Ports

| Service | Port |
|---|---|
| Content Engine | 8001 |
| Demo Classifier | 8000 |

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system design.

## Moving to a server later

All paths are relative. Copy the project folder, install requirements, set env vars or use the Settings UI for keys, and run `python3 app.py`. No hardcoded local paths.
