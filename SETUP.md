# Storylane Customer Insights Content Engine — Setup Guide

> For: anyone receiving this tool for the first time
> Time to set up: ~5 minutes

---

## What you're getting

Two tools that work together:

- **Content Engine** — the main tool. Turns customer call recordings into SEO articles. Runs in your browser at `http://localhost:8001`.
- **Demo Classifier** — a companion tool the engine uses to find relevant Storylane product demos for each article. Runs at `http://localhost:8000`.

---

## Step 1 — Unzip

Unzip the file you received. Inside you'll find a `Storylane Customer Insights Content Engine - Complete/` folder containing:

```
Storylane Customer Insights Content Engine - Complete/
├── ▶ Start Here — Mac Setup.command
├── README.txt
├── Storylane Customer Insights Content Engine/
└── storylane-demo-classifier/
```

Keep everything together — don't move the two tool folders apart.

---

## Step 2 — Mac security fix (do this once)

> **Skip this step entirely if you received the zip via AirDrop or USB** — macOS only quarantines files downloaded from the internet.

macOS blocks downloaded launcher files by default. The `▶ Start Here — Mac Setup.command` file is there to fix this — but because it's also a launcher file, it has the same restriction. You need to open it once with a right-click:

1. **Right-click** `▶ Start Here — Mac Setup.command`
2. Click **Open**
3. Click **Open** again in the dialog that appears
4. A terminal window runs briefly and closes

After this, every other launcher in the folder works with a normal double-click forever.

**Prefer Terminal?** Open Terminal and paste this instead (replace the path with wherever you unzipped):
```
xattr -dr com.apple.quarantine ~/Downloads/"Storylane Customer Insights Content Engine - Complete"
```

---

## Step 3 — Start the Demo Classifier

1. Open `storylane-demo-classifier/`
2. Double-click **`Start Classifier.command`**
3. **First time only:** it installs dependencies automatically — including a browser engine (Playwright/Chromium) which is a large download. This takes **3–5 minutes**. Let it finish, you'll see "Setup complete".
4. A browser tab opens at `http://localhost:8000`
5. Optionally paste your **Anthropic API key** in the field at the top — without it, the content engine falls back to keyword-based demo matching using the existing index, which still works well
6. Minimize this window — keep it running in the background

> **Note:** Demo screenshot previews won't show (too large to include in the zip) but all classified demo data is there. The classifier has a built-in option to re-capture missing screenshots if needed.

---

## Step 4 — Start the Content Engine

1. Open `Storylane Customer Insights Content Engine/`
2. Double-click **`Content Engine.command`**
3. **First time only:** installs automatically (~1 min)
4. A browser tab opens at `http://localhost:8001`

---

## Step 5 — Add your API key in the Content Engine

1. Click **Settings** (top nav)
2. Scroll to **API Keys**
3. Paste your **Anthropic API key** (`sk-ant-...`) → click **Save Keys**
4. Optional: Ahrefs token for keyword volume data; Grain token for automatic call ingestion

> The classifier and the content engine each need the API key entered separately — they store it independently.

That's it. All the articles, insights, and company knowledge from the previous user are already there.

---

## Day-to-day

- Double-click the two `.command` files whenever you want to use the tools
- Close the terminal windows to stop them
- All changes save automatically

---

## Passing it on to someone else

1. In the Content Engine browser → **Settings → Export & Share Tool**
2. Click **Export Both Tools** — downloads a new zip with your data, their API keys stripped
3. Send the zip — recipient follows this same guide

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "cannot be opened" on a launcher | Right-click → Open → Open (or re-run the Mac Setup script) |
| Port already in use | Close any previous terminal windows for these tools, try again |
| Python 3 not found | Install from [python.org/downloads](https://www.python.org/downloads/) |
| Classifier not connecting | Make sure both terminal windows are open and running simultaneously |
| API errors | Check Settings → API Keys — the status indicator turns green when a key is set |
