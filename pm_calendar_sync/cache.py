"""Committed JSON cache files (cache/) used by the fast run modes.

The full convergent sweep is the sole authority for correctness; these caches
are a performance layer on top of it.  Every reader must tolerate a missing or
corrupt file and fall back to a live AppFolio pull — a wiped cache/ directory
must never crash a run (it self-heals on the next write).

Files (committed back to the repo by the workflow, like state.json):
  cache/directories.json — {"refreshed_at", "tenants": [...],
                            "property_groups": [...]}
      Raw tenant_directory / property_group_directory report rows; refreshed
      nightly.  (Pre-cutover caches held an "owners" key instead of
      "property_groups"; the validity check treats those as stale and
      falls back to a live pull, which rewrites the new shape.)
  cache/rent_roll.json   — {"refreshed_at", "rows": [...]}
      Raw rent_roll report rows; rewritten by every mode that pulls rent_roll.
      Baseline for update-mode money diffs and submit-mode balances.

Raw rows are stored verbatim so transforms.build_group_property_map /
build_tenant_info_map produce identical maps from cache or live data.
"""
import json
import os
from pathlib import Path
from typing import Optional

from .config import log

CACHE_DIR        = Path("cache")
DIRECTORIES_FILE = CACHE_DIR / "directories.json"
RENT_ROLL_FILE   = CACHE_DIR / "rent_roll.json"


def load_json(path: Path) -> Optional[dict]:
    """Read a cache file; None (with a warning) when missing/corrupt/non-dict."""
    if not path.exists():
        log.warning(f"Cache file {path} missing — falling back to live data")
        return None
    try:
        raw = path.read_bytes()
        # Same BOM sniffing as state.json: PowerShell redirects on the dev
        # machine can rewrite the file's encoding.
        if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
            text = raw.decode("utf-16")
        elif raw.startswith(b"\xef\xbb\xbf"):
            text = raw.decode("utf-8-sig")
        else:
            text = raw.decode("utf-8")
        data = json.loads(text) if text.strip() else None
    except (OSError, ValueError) as e:
        log.warning(f"Cache file {path} unreadable ({e}) — falling back to live data")
        return None
    if not isinstance(data, dict):
        log.warning(f"Cache file {path} malformed — falling back to live data")
        return None
    return data


def save_json(path: Path, data: dict) -> None:
    """Atomic write (same-dir tmp + os.replace) so a crash mid-write can never
    leave a truncated cache behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
