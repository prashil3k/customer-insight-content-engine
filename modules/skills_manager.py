"""
Skills Manager — upload, store, and retrieve content rubric/skills files.

Skills are text-based rubric instructions (QC, SEO, or draft guidance) uploaded
as .txt or .md files. Claude extracts the useful rubric content on upload.
Each skill can be toggled on/off per use case independently.
"""

import json
import time
import uuid
import threading
from pathlib import Path
import config
from modules.model_manager import create_message

_skills_lock = threading.Lock()
SKILLS_PATH = config.DATA_DIR / "skills.json"


def _load_skills() -> list:
    if SKILLS_PATH.exists():
        try:
            return json.loads(SKILLS_PATH.read_text()).get("skills", [])
        except Exception:
            pass
    return []


def _save_skills(skills: list):
    SKILLS_PATH.write_text(json.dumps({"skills": skills}, indent=2))


def get_skills() -> list:
    return _load_skills()


def get_active_skills(use_case: str) -> list:
    """Return skills active for a given use case: 'qc', 'seo', or 'draft'."""
    key = f"active_{use_case}"
    return [s for s in _load_skills() if s.get(key, False)]


def build_skills_block(use_case: str) -> str:
    """Build the skills injection block for a prompt. Empty string if no active skills."""
    skills = get_active_skills(use_case)
    if not skills:
        return ""
    lines = [f"\n=== ADDITIONAL SKILLS / RUBRICS ({use_case.upper()}) ===",
             "Apply ALL of the following skill checks in addition to the base rubric above.",
             "Each skill has a specific focus area — apply each independently.\n"]
    for s in skills:
        lines.append(f"--- {s['name']} ---")
        lines.append(s["content"])
        lines.append("")
    lines.append("=== END SKILLS ===")
    return "\n".join(lines)


def upload_skill(filename: str, raw_text: str, progress_cb=None) -> dict:
    """
    Process an uploaded skills file. Uses Haiku to extract the rubric content,
    give it a clear name, and strip any boilerplate/marketing from the file.
    Returns the saved skill dict.
    """
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    _p(f"Processing skills file '{filename}'...")

    prompt = f"""A user has uploaded a skills or rubric file for content quality checking. Your job is to extract only the actionable rubric/checklist content from it — strip any preamble, author bios, marketing text, or meta-commentary.

FILENAME: {filename}

FILE CONTENT:
{raw_text[:10000]}

Extract and return a JSON object with:
- "name": A short, clear name for this skill (e.g. "Hook Quality Check", "EEAT Validator", "CTA Strength Rubric"). Max 50 chars.
- "description": One sentence on what this skill checks for.
- "content": The cleaned, actionable rubric content only. Keep all specific checks, criteria, and instructions. Remove author names, tool descriptions, meta-commentary. Format as clear prose or a numbered/bulleted list. Max 1500 words.
- "suggested_use": Which use case this is primarily for — "qc", "seo", "draft", or "all"

Return ONLY the JSON object."""

    response = create_message("haiku", max_tokens=2000, messages=[{"role": "user", "content": prompt}])
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()

    extracted = json.loads(raw)
    suggested = extracted.get("suggested_use", "qc")

    skill = {
        "id": str(uuid.uuid4())[:8],
        "name": extracted.get("name", filename),
        "description": extracted.get("description", ""),
        "content": extracted.get("content", raw_text[:1500]),
        "source_file": filename,
        "active_qc": suggested in ("qc", "all"),
        "active_seo": suggested in ("seo", "all"),
        "active_draft": suggested in ("draft", "all"),
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    with _skills_lock:
        skills = _load_skills()
        skills.append(skill)
        _save_skills(skills)

    _p(f"Skill '{skill['name']}' saved.")
    return skill


def update_skill(skill_id: str, updates: dict) -> bool:
    """Update toggles or name for a skill. Returns True if found."""
    allowed = {"active_qc", "active_seo", "active_draft", "name"}
    with _skills_lock:
        skills = _load_skills()
        for s in skills:
            if s["id"] == skill_id:
                for k, v in updates.items():
                    if k in allowed:
                        s[k] = v
                _save_skills(skills)
                return True
    return False


def delete_skill(skill_id: str) -> bool:
    with _skills_lock:
        skills = _load_skills()
        before = len(skills)
        skills = [s for s in skills if s["id"] != skill_id]
        if len(skills) < before:
            _save_skills(skills)
            return True
    return False
