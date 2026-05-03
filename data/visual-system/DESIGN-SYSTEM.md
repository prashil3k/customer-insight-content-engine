# LinkedIn Visual System — Storylane Design Documentation

## Overview

A system for creating high-quality LinkedIn visual infographics using Claude Code, following Storylane's brand guidelines (v2.0, sourced from the official brand book PDF). Each visual is a self-contained HTML file, screenshotted from the browser and uploaded to LinkedIn. Triggered via `/visual`.

## Templates

All templates at `~/Documents/Claude Code/storylane-visual-system/`:

| Format | Template File | Dimensions |
|--------|--------------|------------|
| Resource guide | resource-guide-template.html | 900×1500 |
| Comparison | comparison-template.html | 900×Auto |
| Process breakdown | process-breakdown-template.html | 900×Auto |
| Data viz - curve | data-viz-curve-template.html | 900×Auto |
| Data viz - pie/donut | data-viz-pie-template.html | 900×Auto |
| Data viz - bar chart | data-viz-bar-template.html | 900×Auto |
| Data viz - scatter | data-viz-scatter-template.html | 900×Auto |
| Carousel | carousel-template.html | 900×900 per slide |

## Dimensions

- **Resource guide:** 900px wide × 1500px tall (portrait)
- **All other formats:** 900px wide, height auto (hugs content)
- **Carousels:** 900×900 per slide, all slides in one HTML
- **Never use 1080px wide**

---

## Brand: Storylane (source: Brand Book v2.0 PDF)

### Full Colour Palette (15 colours)

| Name | Hex | Group |
|------|-----|-------|
| Garnet Red Dark | `#13010C` | Dark base |
| Garnet Red | `#390323` | Dark base |
| Garnet Red Light | `#61053C` | Dark base |
| Pink | `#ED2DA0` | Pop |
| Purple | `#7948B5` | Pop |
| Yellow | `#FFD863` | Pop |
| Orange | `#FF7F51` | Pop |
| Light Pink | `#FFBDE5` | Light accent |
| Light Purple | `#DDC1FF` | Light accent |
| Light Yellow | `#F0ECC7` | Light accent |
| Pale Pink | `#FEECF7` | Pale accent |
| Pale Purple | `#FBEAFF` | Pale accent |
| Pale Yellow | `#FAF9F0` | Pale accent |
| Green | `#1E6263` | Accent |
| Pale Blue | `#E4F7F7` | Pale accent |

### Approved Colour Combinations (from brand book page 5)

Each row = background colour → approved colours to use on top of it.

| Background | Approved accent colours |
|------------|------------------------|
| Garnet Red `#390323` | Pink, Light Purple, Yellow, Green, Orange |
| Pale Yellow `#FAF9F0` | Garnet Red, Pink, Purple, Green, Orange |
| Yellow `#FFD863` | Garnet Red, Light Purple, Green, Orange |
| Purple `#7948B5` | Garnet Red, Light Purple, Light Pink, Pale Blue |
| Green `#1E6263` | Garnet Red, Pale Blue, Light Pink, Light Purple |
| Orange `#FF7F51` | Garnet Black, Garnet Red, Yellow, Green |

**Rule:** Only combine colours from the same row. Never pair two pop colours against each other as background + text.

### Typography (source: Brand Book v2.0 PDF)

| Font | Role | Notes |
|------|------|-------|
| **Inter Display** | Headlines, titles, body text — primary | Normal weight |
| **Inter Display Italics** | Highlighted/key words only | Used for emphasis words, shown in Yellow on dark backgrounds in the brand book |
| **Inter Tight** | Google Slides alternative only | Not for LinkedIn visuals |

**Key pairing pattern from brand book:** In headlines, specific key words are set in Inter Display Italics and coloured Yellow (`#FFD863`) on dark backgrounds — e.g. "The *quick* brown fox jumps over the *lazy* dog" where *quick* and *lazy* are italic + yellow.

Font files: `~/Library/Fonts/InterVariable.ttf`, `~/Library/Fonts/InterVariable-Italic.ttf`, `~/Library/Fonts/Inter.ttc`

All fonts must be base64-embedded in generated HTML so files are fully self-contained.

---

## Logos

### Storylane logos (from Brandfetch, in logos/ folder)

| File | Use |
|------|-----|
| `storylane_full_logo.svg` | Full logo with wordmark (light version) |
| `storylane_logo_black.svg` | Full logo with wordmark (dark/black text version) |
| `storylane_icon.png` | Icon only, 256×256px |

Use `storylane_full_logo.svg` on dark backgrounds, `storylane_logo_black.svg` on light backgrounds.

### Rules

1. **Always use real logos.** Never hand-draw SVGs or use placeholders.
2. **Always base64-embed.** No external URLs.
3. **Display at 30×30px** inside 40×40px rounded tiles with Pale Pink (`#FEECF7`) background.

### Sources (in priority order)

1. `~/Documents/Claude Code/storylane-visual-system/logos/` — pre-downloaded
2. Wikimedia Commons — best for full-colour SVGs
3. Company's own CDN
4. Google favicon service — `google.com/s2/favicons?domain=X&sz=128` as fallback

### Pre-downloaded logos (logos/ folder)

Storylane: storylane_full_logo.svg, storylane_logo_black.svg, storylane_icon.png
SaaS ecosystem: salesforce, hubspot, gong, outreach, intercom, apollo, g2, marketo, drift
Google Workspace: gmail, sheets, docs, slides, drive, calendar, chrome
Others: slack, stripe, linkedin, notion, linear, amplitude, greenhouse

---

## Colour Application Rules

- **Header bg:** Garnet Red `#390323` or Garnet Black `#13010C`
- **Header title:** White text; key words in Inter Display Italics + Yellow `#FFD863`
- **Badge pill:** Pink `#ED2DA0`
- **Row numbers / step numbers:** Pink `#ED2DA0`
- **Column header pills:** Purple `#7948B5`
- **Body text on light bg:** `rgba(57, 3, 35, 0.85)` (Garnet Red at opacity)
- **Muted text:** `rgba(57, 3, 35, 0.45)` — never arbitrary gray hex codes
- **Logo tiles:** Pale Pink `#FEECF7` background
- **Tags / pills on dark:** Garnet Red background, White text
- **Bottom info cards:** Light Pink `#FFBDE5` or Light Purple `#DDC1FF`
- **CTA button:** Pink `#ED2DA0`
- **Content area background:** Pale Yellow `#FAF9F0` or Pale Pink `#FEECF7`
- **Row dividers:** `rgba(237, 45, 160, 0.1)`
- **Chart primary colour:** Pink `#ED2DA0`; secondary: Purple `#7948B5`

---

## Format Specs

### Resource Guide
- 5-column grid: #, Tool, What it does, Connects to, Impact
- Grid columns: `50px 180px 1fr 170px 120px`
- Bottom info cards (3×, Light Pink bg)
- CTA bar: Garnet Black bg, Pink button

### Comparison
- 3-column grid: Label (200px), Option A (Light Pink bg), Option B (Light Purple bg)
- Column header pills: Purple `#7948B5`

### Process Breakdown
- Two-column: steps list (left) + info cards (right, 320px)
- Step dots: Pink `#ED2DA0`
- Right cards: Garnet Black / Light Pink / Light Purple

### Data Visualizations
- All charts use **SVG** — one coordinate system, never mix SVG + absolute HTML
- Curve: Pink line + gradient fill on white/pale bg
- Pie/donut: brand colours for segments, center stat
- Bar: colour gradient (Pink → Purple → Yellow), values inside bars
- Scatter: dot categories in Pink/Purple/Yellow, trend line dashed, annotation boxes

### Carousel
- 900×900px per slide, all in one HTML
- Alternate backgrounds: Garnet Dark / Pale Yellow / Purple for rhythm
- Titles 52px+, body 20px+ — fill the space
- STORYLANE watermark on every slide (bottom right, low opacity)
- No slide numbers or meta badges

---

## Quality Checklist

- [ ] All logos are real and base64-embedded
- [ ] All colours are from the 15-colour Storylane palette
- [ ] Colour combinations follow the approved pairings from the brand book
- [ ] No dead whitespace — increase font size or add content to fill
- [ ] Fonts are Inter Display + Inter (embedded, not CDN)
- [ ] Key/highlight words in headlines use Inter Display Italics + Yellow on dark bgs
- [ ] SVG charts use one coordinate system
- [ ] Run evaluator subagent and fix all issues before presenting

## Key Lessons

1. Never fake logos. Use files from `logos/` folder first, then Wikimedia, then Google favicons.
2. Always base64-embed everything (fonts, logos). External URLs break when offline.
3. "Too much whitespace" → bigger text or more content, never remove padding.
4. SVG for charts, CSS grid for structured layouts.
5. Colour combinations must come from the approved pairings table, not intuition.
