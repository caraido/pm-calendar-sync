"""
resolve_state_conflict.py
─────────────────────────
Resolves git merge conflict markers in state.json by keeping the HEAD
version (your local repair) and discarding the remote version.

Usage:
    cd D:\Repos\pm-calendar-sync
    python resolve_state_conflict.py
"""

import re, json
from pathlib import Path

STATE_FILE = Path("state.json")
raw = STATE_FILE.read_text(encoding="utf-8")

# Count conflict markers
conflicts = raw.count("<<<<<<< HEAD")
if conflicts == 0:
    print("No merge conflicts found in state.json.")
    # Try parsing anyway
    try:
        json.loads(raw)
        print("✅ File is valid JSON.")
    except json.JSONDecodeError as e:
        print(f"❌ But JSON is still invalid: {e}")
    exit(0)

print(f"Found {conflicts} merge conflict(s) in state.json")

# Resolve all conflicts by keeping HEAD (the repaired/local version)
# Pattern: <<<<<<< HEAD\n{HEAD content}\n=======\n{remote content}\n>>>>>>> {hash}
resolved = re.sub(
    r'<<<<<<< HEAD\n(.*?)\n=======\n.*?\n>>>>>>> [^\n]+',
    r'\1',
    raw,
    flags=re.DOTALL,
)

# Verify no conflict markers remain
remaining = resolved.count("<<<<<<< HEAD")
if remaining > 0:
    print(f"⚠️  {remaining} conflict(s) still remain — trying aggressive cleanup")
    # More aggressive: handle any remaining markers
    resolved = re.sub(r'<<<<<<< [^\n]*\n', '', resolved)
    resolved = re.sub(r'=======\n', '', resolved)
    resolved = re.sub(r'>>>>>>> [^\n]*\n', '', resolved)

# Fix any trailing commas that the merge may have introduced
resolved = re.sub(r',\s*([\]}])', r'\1', resolved)

# Try to parse
try:
    data = json.loads(resolved)
    print(f"✅ Conflicts resolved! {len(data)} top-level keys.")

    # Quick sanity check on commitments
    comms = data.get("_commitments", {})
    total_entries = sum(len(v) for v in comms.values())
    bare_keys = [k for k in comms if "@" not in k]
    print(f"   Commitments: {len(comms)} keys, {total_entries} entries")
    if bare_keys:
        print(f"   ⚠️  Still has {len(bare_keys)} bare-oid keys: {bare_keys}")
        print(f"   Run repair_state.py after this to clean them up.")
    else:
        print(f"   ✅ No bare-oid keys (repair was preserved)")

    # Write clean JSON
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"\n   state.json rewritten cleanly.")
    print(f"   Next steps:")
    print(f"     1. Disable the GitHub Actions workflow (Actions → ⋯ → Disable)")
    print(f"     2. git add state.json && git commit -m 'fix: resolve merge conflict'")
    print(f"     3. git push")
    print(f"     4. Re-enable the workflow")

except json.JSONDecodeError as e:
    print(f"\n❌ Still broken after conflict resolution: {e}")
    print(f"   Line {e.lineno}, col {e.colno}")
    # Show context
    start = max(0, e.pos - 300)
    end   = min(len(resolved), e.pos + 300)
    print(f"\n── Context ──")
    print(resolved[start:end])

    # Write the partially-fixed version for manual inspection
    Path("state_partially_fixed.json").write_text(resolved, encoding="utf-8")
    print(f"\n   Wrote state_partially_fixed.json for manual inspection.")