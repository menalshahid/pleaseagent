#!/usr/bin/env python3
"""
Daily IST.edu.pk → all_kb.txt sync (replaces only the auto-scraped section).

- Inserts ## [AUTO_SCRAPED_IST_WEB_START] / END markers on first run (manual FAQ + voice FAQ preserved).
- Re-downloads configured pages, extracts main text with trafilatura, writes a single fresh scraped block.
- Backups previous all_kb.txt to backup_kb/ before overwrite.

Schedule (cron / Task Scheduler), example 03:00 daily:
  cd /path/to/purple && python ist_kb_sync.py
Then reload the running app (see app.py POST /api/admin/reload-kb) or restart gunicorn.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Defaults (edit or set IST_SCRAPE_URLS as comma-separated URLs) ────────────
DEFAULT_URLS = [
    "https://ist.edu.pk/",
    "https://ist.edu.pk/admission",
    "https://ist.edu.pk/admission?section=general-eligibility",
    "https://ist.edu.pk/admission?section=entry-test",
    "https://ist.edu.pk/admission?section=merit-determination-criteria",
    "https://ist.edu.pk/admission?section=fee-structure-bs-programs",
    "https://ist.edu.pk/admission?section=financial-aid-scholarships",
    "https://ist.edu.pk/admission?section=admission-faq",
    "https://ist.edu.pk/programs",
]

START_MARK = "## [AUTO_SCRAPED_IST_WEB_START]"
END_MARK = "## [AUTO_SCRAPED_IST_WEB_END]"
VOICE_ANCHOR = "## KICSIT DIRECTOR & MERIT SCHOLARSHIPS (VOICE FAQ)"
KB_PATH = Path(__file__).resolve().parent / "all_kb.txt"
BACKUP_DIR = Path(__file__).resolve().parent / "backup_kb"


def _urls_from_env() -> list[str]:
    raw = os.getenv("IST_SCRAPE_URLS", "").strip()
    if not raw:
        return list(DEFAULT_URLS)
    return [u.strip() for u in raw.split(",") if u.strip()]


def migrate_insert_markers(text: str) -> str:
    """First-time: wrap auto-sync region so we can replace it without duplicating manual KB."""
    if START_MARK in text and END_MARK in text:
        return text

    if VOICE_ANCHOR not in text:
        raise ValueError(
            f"Cannot migrate: anchor {VOICE_ANCHOR!r} not found — add markers manually."
        )

    t = text
    needle = (
        "======================================================================\n"
        "## PROGRAMS AND ADMISSIONS DATA\n"
        "======================================================================\n"
        "--- Admissions ---\n"
    )
    if needle in t and START_MARK not in t:
        t = t.replace(
            needle,
            needle
            + START_MARK
            + "\n# Auto-synced from ist.edu.pk — replaced by ist_kb_sync.py; do not edit by hand.\n\n",
            1,
        )

    if END_MARK not in t:
        t = t.replace(
            "\n## KICSIT DIRECTOR & MERIT SCHOLARSHIPS (VOICE FAQ)",
            "\n" + END_MARK + "\n\n## KICSIT DIRECTOR & MERIT SCHOLARSHIPS (VOICE FAQ)",
            1,
        )

    if START_MARK not in t or END_MARK not in t:
        raise ValueError("Migration failed: START/END markers still missing.")
    return t


def split_kb(text: str) -> tuple[str, str] | None:
    """Return (before START marker, from END marker inclusive to EOF) or None."""
    if START_MARK not in text or END_MARK not in text:
        return None
    i0 = text.index(START_MARK)
    i1 = text.index(END_MARK)
    head = text[:i0]
    tail = text[i1:]
    return head, tail


def fetch_text(url: str) -> str | None:
    try:
        import trafilatura
    except ImportError:
        print("Install trafilatura: pip install trafilatura", file=sys.stderr)
        raise

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    text = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    return (text or "").strip()


def build_scraped_block(urls: list[str]) -> str:
    parts: list[str] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts.append(f"# Auto-sync UTC: {ts}\n")
    seen_hash: set[str] = set()

    for url in urls:
        time.sleep(1.0)
        try:
            body = fetch_text(url)
        except Exception as e:
            parts.append(f"\n--- FETCH ERROR: {url} — {e}\n")
            continue
        if not body or len(body) < 80:
            parts.append(f"\n--- EMPTY/SHORT: {url}\n")
            continue
        h = hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()[:16]
        if h in seen_hash:
            continue
        seen_hash.add(h)
        safe_title = re.sub(r"[^\w\-./:?=&]+", " ", url)[:120].strip()
        parts.append("\n" + "=" * 70 + "\n")
        parts.append(f"[TOPIC: {safe_title}]\n")
        parts.append(body)
        parts.append("\n")

    return "\n".join(parts).strip() + "\n"


def sync_kb(
    kb_path: Path = KB_PATH,
    dry_run: bool = False,
    force: bool = False,
) -> bool:
    kb_path = kb_path.resolve()
    text = kb_path.read_text(encoding="utf-8")
    text = migrate_insert_markers(text)
    split = split_kb(text)
    if split is None:
        raise RuntimeError("Markers present but split failed.")

    head, tail = split
    urls = _urls_from_env()
    new_middle = build_scraped_block(urls)

    old_middle = ""
    try:
        s = text.index(START_MARK) + len(START_MARK)
        e = text.index(END_MARK)
        old_middle = text[s:e]
    except ValueError:
        pass

    if not force and hashlib.sha256(old_middle.encode()).digest() == hashlib.sha256(
        new_middle.encode()
    ).digest():
        print("No content change; skipping write.")
        return False

    out = head + START_MARK + "\n" + new_middle + "\n" + tail

    if dry_run:
        print("--- dry-run: would write", len(out), "bytes ---")
        return True

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(kb_path, BACKUP_DIR / f"all_kb_{stamp}.txt")

    kb_path.write_text(out, encoding="utf-8")
    print("Wrote", kb_path, "backup:", BACKUP_DIR / f"all_kb_{stamp}.txt")
    return True


def main() -> None:
    p = argparse.ArgumentParser(description="Sync IST.edu.pk into all_kb.txt scraped section.")
    p.add_argument("--dry-run", action="store_true", help="Do not write file")
    p.add_argument("--force", action="store_true", help="Write even if hash unchanged")
    p.add_argument("--kb", type=Path, default=KB_PATH, help="Path to all_kb.txt")
    args = p.parse_args()
    try:
        sync_kb(kb_path=args.kb, dry_run=args.dry_run, force=args.force)
    except Exception as e:
        print("Error:", e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
