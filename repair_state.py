"""
repair_state.py
───────────────
One-time repair: fixes the commitment duplication bug in state.json.

Problems fixed:
  1. Bare-oid commitment entries (e.g. "83") duplicating into owner-scoped
     entries (e.g. "83@10", "83@11") every run.
  2. Commitments missing calendar_id, causing them to be processed for
     every calendar and triggering the ≥1-promise recreation cycle.
  3. Massive duplication (oid=83 has 21+ bare + 23+ scoped entries).

Strategy:
  - For each bare-oid commitment, try to match it to an owner-scoped key
    that already has the same event_id. If found, delete the bare copy.
  - If no match, delete the bare copy (it's orphaned — the owner-scoped
    version already has its own events).
  - Deduplicate all owner-scoped entries by event_id.
  - Report what was cleaned up.

Usage:
    cd D:\\Repos\\pm-calendar-sync
    python repair_state.py

Then commit + push state.json.
"""

import json
from pathlib import Path
from collections import Counter

STATE_FILE = Path("state.json")

if not STATE_FILE.exists():
    print("state.json not found — run from the repo root.")
    exit(1)

state = json.loads(STATE_FILE.read_text())
commitments = state.get("_commitments", {})

print(f"Total commitment keys before: {len(commitments)}")
total_entries_before = sum(len(v) for v in commitments.values())
print(f"Total commitment entries before: {total_entries_before}")

# ── Step 1: Identify bare-oid vs owner-scoped keys ──────────────────────
bare_keys = [k for k in commitments if "@" not in k]
scoped_keys = [k for k in commitments if "@" in k]

print(f"\nBare-oid keys: {len(bare_keys)} → {bare_keys}")
print(f"Owner-scoped keys: {len(scoped_keys)}")

# ── Step 2: Remove ALL bare-oid entries ──────────────────────────────────
# These are legacy entries from before owner-scoping was added.  Every
# legitimate commitment should already exist under an owner-scoped key
# (created on the run that added calendar_id tracking or discovered as
# a "split copy" on a subsequent run).  The bare entries serve no purpose
# and cause the duplication loop.
removed_bare = 0
for key in bare_keys:
    entries = commitments.pop(key, [])
    removed_bare += len(entries)
    print(f"  Removed bare key '{key}': {len(entries)} entries")

# ── Step 3: Deduplicate owner-scoped entries by event_id ─────────────────
deduped = 0
for key in list(commitments.keys()):
    entries = commitments[key]
    seen_ids = set()
    unique = []
    for c in entries:
        eid = c.get("event_id")
        if eid and eid not in seen_ids:
            seen_ids.add(eid)
            unique.append(c)
        else:
            deduped += 1
    commitments[key] = unique

# ── Step 4: Report per-key counts ────────────────────────────────────────
print(f"\n{'─' * 50}")
print("After cleanup — commitment counts per key:")
print(f"{'─' * 50}")
for key in sorted(commitments.keys()):
    entries = commitments[key]
    print(f"  {key:20s}: {len(entries)} commitment(s)")
    for c in entries:
        anchor = c.get('anchor_date', '?')
        cal = 'SET' if c.get('calendar_id') else 'NOT SET'
        covers = c.get('covers_rent_month', '—')
        print(f"    anchor={anchor}  cal_id={cal}  covers={covers}")

# ── Step 5: Save ─────────────────────────────────────────────────────────
total_entries_after = sum(len(v) for v in commitments.values())
# Remove empty keys
commitments = {k: v for k, v in commitments.items() if v}
state["_commitments"] = commitments

STATE_FILE.write_text(json.dumps(state, indent=2))

print(f"\n{'═' * 50}")
print(f"SUMMARY")
print(f"{'═' * 50}")
print(f"Bare-oid entries removed:    {removed_bare}")
print(f"Duplicate entries removed:   {deduped}")
print(f"Entries before:              {total_entries_before}")
print(f"Entries after:               {total_entries_after}")
print(f"Keys remaining:              {len(commitments)}")
print(f"\nstate.json saved. Commit + push to apply.")
print(f"\nNOTE: After pushing, run a normal sync (not FORCE_REFRESH).")
print(f"The sync will re-discover any legitimate commitment events on")
print(f"each calendar and set their calendar_id properly.")