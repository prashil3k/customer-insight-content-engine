#!/usr/bin/env python3
"""
One-time migration: Import existing demo_report.json into the new persistent index.
Run this once: python3 migrate_to_index.py
"""

import json
from pathlib import Path
from datetime import datetime

PROJECT_DIR = Path(__file__).parent
REPORT_FILE = PROJECT_DIR / "output" / "demo_report.json"
INDEX_FILE = PROJECT_DIR / "demo_index.json"


def migrate():
    if not REPORT_FILE.exists():
        print("No demo_report.json found -- nothing to migrate.")
        return

    # Load existing report
    with open(REPORT_FILE) as f:
        demos = json.load(f)

    print(f"Found {len(demos)} demos in existing report.")

    # Load or create index
    if INDEX_FILE.exists():
        with open(INDEX_FILE) as f:
            index = json.load(f)
        print(f"Existing index has {len(index.get('demos', {}))} demos.")
    else:
        index = {"version": 2, "last_sync": None, "demos": {}}

    added = 0
    for d in demos:
        # Generate key from demo URL or showcase URL
        url = d.get("demo_url", "") or d.get("showcase_url", "")
        if not url:
            continue

        key = url.split("?")[0].split("#")[0].rstrip("/")
        if "://" in key:
            key = key.split("://", 1)[1]
        key = key.lower()

        if key in index["demos"]:
            print(f"  = {d.get('name', '?')} (already in index)")
            continue

        # Convert steps format
        steps_text = []
        for s in d.get("steps", []):
            steps_text.append({
                "step": s.get("step_number", 0),
                "total": s.get("total_steps", 0),
                "text": s.get("tooltip_text", ""),
            })

        entry = {
            "key": key,
            "name": d.get("name", "Unknown"),
            "showcase_url": d.get("showcase_url", ""),
            "demo_url": d.get("demo_url", ""),
            "live_preview_url": d.get("live_preview_url", ""),
            "category": d.get("category", ""),
            "is_accessible": d.get("is_accessible", True),
            "is_gated": d.get("is_gated", False),
            "error": d.get("error", ""),
            "total_steps": d.get("total_steps", 0),
            "steps_captured": d.get("steps_captured", 0),
            "steps_text": steps_text,
            "classification": d.get("classification", {}),
            "tags": [],
            "source": "showcase",
            "discovered_at": datetime.now().isoformat(),
            "last_scanned_at": datetime.now().isoformat() if d.get("steps_captured", 0) > 0 else None,
            "scan_count": 1 if d.get("steps_captured", 0) > 0 else 0,
        }

        index["demos"][key] = entry
        added += 1
        print(f"  + {entry['name']}")

    index["last_sync"] = datetime.now().isoformat()

    with open(INDEX_FILE, "w") as f:
        json.dump(index, f, indent=2)

    print(f"\nMigration complete: {added} demos imported, {len(index['demos'])} total in index.")


if __name__ == "__main__":
    migrate()
