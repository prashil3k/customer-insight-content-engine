import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

import pandas as pd

import config
from modules.insight_extractor import _init_db, _source_already_processed, _mark_source_processed
from modules.model_manager import create_message

SKIP_SHEETS = {
    'Legend', 'Health Check Log', 'Sources & Update Log',
    'Future Intelligence Layers', 'Quick Comparison', 'Storylane Profile',
    'Comparison Matrix',
}

_BAD_COMP_WORDS = {'narrative', 'pattern', 'log', 'claim', 'change log', 'marketing', 'overview', 'summary', 'date'}


def _is_valid_competitor_name(name: str) -> bool:
    if not name or name.strip() in ('', 'nan', 'Competitor'):
        return False
    name = name.strip()
    if name.lower() == 'storylane':
        return False
    if re.match(r'^\d{4}-\d{2}-\d{2}', name):
        return False
    if '—' in name or len(name) > 60:
        return False
    if name == name.upper() and len(name) > 4:
        return False
    name_lower = name.lower()
    if any(w in name_lower for w in _BAD_COMP_WORDS):
        return False
    return True


def _normalize_competitor_name(name: str) -> str:
    """Strip variant suffixes so Tourial / Navless and Tourial (Now Navless.Ai) both → Tourial."""
    name = re.sub(r'\s*\(.*?\)\s*$', '', name).strip()
    name = re.sub(r'\s*/.*$', '', name).strip()
    return name.strip()


def _merge_competitor_chunks(raw: dict) -> dict:
    """Case-insensitive merge + normalize names. HowdyGo + Howdygo → one entry."""
    merged = {}     # canonical_lower → (display_name, [lines])
    for comp, lines in raw.items():
        norm = _normalize_competitor_name(comp)
        key = norm.lower()
        if key not in merged:
            merged[key] = (norm, [])
        merged[key][1].extend(lines)
    return {display: lines for display, lines in merged.values()}


def _find_header_row(df, min_cols=3):
    for i in range(min(6, len(df))):
        if df.iloc[i].notna().sum() >= min_cols:
            return i
    return 0


def _df_with_headers(df):
    h = _find_header_row(df)
    seen = {}
    headers = []
    for j, v in enumerate(df.iloc[h]):
        name = str(v).strip() if pd.notna(v) and str(v).strip() != 'nan' else f'_col{j}'
        count = seen.get(name, 0)
        seen[name] = count + 1
        headers.append(name if count == 0 else f'{name}_{count}')
    data = df.iloc[h + 1:].reset_index(drop=True)
    data.columns = headers
    return data.dropna(how='all')


def _row_to_str(row):
    parts = []
    for k, v in row.items():
        if str(k).startswith('_col'):
            continue
        vs = str(v).strip() if pd.notna(v) else ''
        if vs and vs != 'nan':
            parts.append(f'{k}: {vs}')
    return ' | '.join(parts)


def _sheet_to_competitor_chunks(sheet_name, df):
    """
    Returns {competitor_name: [text_lines]} for all data in this sheet.
    Handles both multi-competitor sheets (Competitor column) and
    single-competitor sheets (sheet name or row-0 metadata).
    """
    if sheet_name in SKIP_SHEETS:
        return {}

    flat = ' '.join(str(v) for v in df.iloc[:4].values.flatten() if pd.notna(v)).lower()
    if any(kw in flat for kw in ['future intelligence', 'health check', 'sources & update', 'legend']):
        return {}

    data = _df_with_headers(df)
    if data.empty:
        return {}

    # Find a "Competitor" column (multi-competitor sheet)
    comp_col = next(
        (c for c in data.columns if c.strip().lower() == 'competitor'),
        None
    )

    if comp_col:
        chunks = {}
        for _, row in data.iterrows():
            comp = str(row[comp_col]).strip()
            if not _is_valid_competitor_name(comp):
                continue
            line = _row_to_str(row)
            if line:
                chunks.setdefault(comp, []).append(line)
        return chunks

    # Single-competitor sheet — derive name from row 0 or sheet name
    row0 = str(df.iloc[0, 0]) if pd.notna(df.iloc[0, 0]) else ''
    if 'competitor:' in row0.lower():
        comp = row0.lower().split('competitor:')[1].split('|')[0].strip().title()
    else:
        comp = sheet_name.replace(' Detail', '').replace(' Profile', '').strip()

    if not comp:
        return {}

    lines = [_row_to_str(row) for _, row in data.iterrows()]
    lines = [l for l in lines if l]
    return {comp: lines} if lines else {}


def _write_competitor_insight(competitor, text_chunks, source_name, source_id_prefix):
    _init_db()
    slug = competitor.lower().replace(' ', '_').replace('/', '_')
    source_id = f'competitor_intel__{source_id_prefix}__{slug}'

    if _source_already_processed(source_id):
        return None

    full_text = '\n'.join(text_chunks)[:7000]

    prompt = f"""You are a competitive intelligence analyst for Storylane (an interactive demo builder SaaS).

Below is structured data about a competitor called "{competitor}" extracted from a competitor fact sheet.

DATA:
{full_text}

Extract insights a B2B content marketer would use when writing articles about Storylane vs competitors.
Return a JSON object with:
- "customer_segment": "Competitive intelligence — {competitor}"
- "pain_points": Array of specific weaknesses or gaps {competitor} has (use language from the data — G2 cons, feature gaps, pricing issues)
- "quotes": Array of objects with "text" (verbatim from G2 or source data), "context", "content_value"
- "use_cases": Array of scenarios where {competitor} wins, and scenarios where Storylane wins against them
- "objections": Array of objections or hesitations prospects raise about {competitor}
- "competitors": ["{competitor}"]
- "metrics": Array of specific numbers (G2 rating, pricing tiers, review count, seat limits, etc.)
- "tags": Array of topic tags — include the competitor name (lowercase), relevant features, and "competitive-intel"
- "confidence": Float 0.7-1.0 (this is structured competitive data so should be high)
- "raw_summary": 2-3 sentence summary of {competitor}'s positioning vs Storylane

Return ONLY the JSON object."""

    response = create_message('haiku', max_tokens=1500, messages=[{'role': 'user', 'content': prompt}])
    raw = response.content[0].text.strip()
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
    raw = raw.strip().rstrip('```').strip()

    data = json.loads(raw)
    insight_id = str(uuid.uuid4())

    conn = sqlite3.connect(str(config.INSIGHTS_DB_PATH))
    conn.execute(
        'INSERT INTO insights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            insight_id,
            source_id,
            'competitor_intel',
            source_name,
            '',
            time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            data.get('customer_segment', f'Competitive intelligence — {competitor}'),
            json.dumps(data.get('pain_points', [])),
            json.dumps(data.get('quotes', [])),
            json.dumps(data.get('use_cases', [])),
            json.dumps(data.get('objections', [])),
            json.dumps(data.get('competitors', [competitor])),
            json.dumps(data.get('metrics', [])),
            json.dumps(data.get('tags', [])),
            data.get('confidence', 0.85),
            json.dumps([]),
            data.get('raw_summary', ''),
            full_text[:8000],
        ),
    )
    conn.commit()
    conn.close()

    _mark_source_processed(source_id, {
        'source_name': source_name,
        'source_type': 'competitor_intel',
        'competitor': competitor,
    })
    return {'id': insight_id, 'competitor': competitor}


def ingest_xlsx(path: str, progress_cb=None) -> dict:
    """
    Read a competitor fact sheet XLSX and write one structured insight per
    competitor into insights.db. Safe to re-run — already-processed
    competitor+file combos are skipped.
    """
    path = Path(path)
    source_name = path.name
    source_id_prefix = path.stem.lower().replace(' ', '_')[:40]

    if progress_cb:
        progress_cb(f'Reading {source_name}...')

    sheets = pd.read_excel(str(path), sheet_name=None, header=None)

    # Collect all text chunks grouped by competitor across all sheets
    raw_chunks = {}
    for sheet_name, df in sheets.items():
        chunks = _sheet_to_competitor_chunks(sheet_name, df)
        if not chunks:
            continue
        if progress_cb:
            progress_cb(f'Parsed sheet: {sheet_name} ({len(chunks)} competitor(s))')
        for comp, lines in chunks.items():
            raw_chunks.setdefault(comp, []).extend(lines)

    competitor_chunks = _merge_competitor_chunks(raw_chunks)

    processed, skipped, errors = 0, 0, []

    for comp, lines in competitor_chunks.items():
        if progress_cb:
            progress_cb(f'Extracting insight for {comp}...')
        try:
            result = _write_competitor_insight(comp, lines, source_name, source_id_prefix)
            if result:
                processed += 1
                if progress_cb:
                    progress_cb(f'  ✓ {comp}')
            else:
                skipped += 1
                if progress_cb:
                    progress_cb(f'  — {comp} already processed, skipped')
        except Exception as e:
            errors.append(f'{comp}: {e}')
            if progress_cb:
                progress_cb(f'  ERROR {comp}: {e}')

    return {'processed': processed, 'skipped': skipped, 'errors': errors}
