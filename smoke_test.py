"""
smoke_test.py — verifies the modular pm_calendar_sync package.

Run from the repo root (the directory containing both sync.py and the
pm_calendar_sync/ package):

    set APPFOLIO_DB_NAME=openkey
    set APPFOLIO_CLIENT_ID=...
    set APPFOLIO_CLIENT_SECRET=...
    set GOOGLE_SERVICE_ACCOUNT_JSON=...      (or a path)
    python smoke_test.py

It does NOT hit AppFolio or Google — the Google client build is mocked and
no network call is made.  It checks:
  1. Every module imports cleanly and the package wires together.
  2. The public classes/functions are reachable.
  3. The four fixes behave correctly (payment color, exclusive end dates,
     credential encoding/strip, BOM-safe state read).

Exit code 0 = all good; non-zero = something regressed.
"""
import os
import sys
import json
import tempfile
import unittest.mock as mock
from pathlib import Path

# Stub env so config.py imports without real secrets / special chars to test encoding
os.environ.setdefault("APPFOLIO_DB_NAME", "openkey")
os.environ.setdefault("APPFOLIO_CLIENT_ID", "id with spaces@weird")
os.environ.setdefault("APPFOLIO_CLIENT_SECRET", "sec/ret:with#chars ")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type":"service_account"}')

failures = []
def check(label, cond):
    print(f"  {'✓' if cond else '✗ FAIL'}  {label}")
    if not cond:
        failures.append(label)

print("=== 1. Package imports (Google build mocked) ===")
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"):
    import pm_calendar_sync
    from pm_calendar_sync import SyncOrchestrator
    from pm_calendar_sync import config, status, transforms, appfolio, \
        calendar_manager, state, orchestrator
    # Instantiating the orchestrator constructs AppFolioClient, the (mocked)
    # GoogleCalendarManager, and StateManager — exercises the full wiring.
    orch = SyncOrchestrator()

check("pm_calendar_sync.SyncOrchestrator importable", SyncOrchestrator is not None)
check("orchestrator instance built", orch is not None)
check("orch.af is AppFolioClient", type(orch.af).__name__ == "AppFolioClient")
check("orch.gcal is GoogleCalendarManager", type(orch.gcal).__name__ == "GoogleCalendarManager")
check("orch.state is StateManager", type(orch.state).__name__ == "StateManager")
check("_gcal_execute shared by manager+orchestrator",
      calendar_manager._gcal_execute is orchestrator._gcal_execute)

print("\n=== 2. FIX 1: payment_status (never red while balance remains) ===")
rent = 1200.0
check("balance 1427 (arrears, paid) -> Partial",
      status.payment_status(rent, 1427.0) == status.STATUS_PARTIAL)
check("balance 1200 (= one month, paid) -> Partial",
      status.payment_status(rent, 1200.0) == status.STATUS_PARTIAL)
check("balance 0 -> Paid", status.payment_status(rent, 0.0) == status.STATUS_PAID)
check("balance -200 -> Prepaid", status.payment_status(rent, -200.0) == status.STATUS_PREPAID)
check("classify_status still red for unpaid 1427",
      status.classify_status(rent, 1427.0) == status.STATUS_UNPAID)

print("\n=== 3. FIX 2: _next_day (exclusive all-day end) ===")
check("2026-06-15 -> 2026-06-16", transforms._next_day("2026-06-15") == "2026-06-16")
check("2026-06-30 -> 2026-07-01", transforms._next_day("2026-06-30") == "2026-07-01")
check("2026-12-31 -> 2027-01-01", transforms._next_day("2026-12-31") == "2027-01-01")
check("2028-02-29 -> 2028-03-01 (leap)", transforms._next_day("2028-02-29") == "2028-03-01")

print("\n=== 4. FIX 3: credential strip + URL-encode ===")
check("secret stripped of trailing space", config.APPFOLIO_CLIENT_SECRET == "sec/ret:with#chars")
check("no raw space in _AF_BASE", " " not in config._AF_BASE)
check("raw id not leaked into URL", "id with spaces" not in config._AF_BASE)
check("host intact", config._AF_BASE.endswith("@openkey.appfolio.com/api/v2/reports"))

print("\n=== 5. FIX 4: BOM-safe state read (all encodings) ===")
data = {"_commitments": {}, "x": "🔴 Unpaid"}
for label, enc in [("plain utf-8", "utf-8"), ("utf-8+BOM", "utf-8-sig"), ("utf-16", "utf-16")]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding=enc) as f:
        json.dump(data, f)
        tmp = f.name
    # Point StateManager at this file and load
    with mock.patch.object(state, "STATE_FILE", Path(tmp)):
        sm = state.StateManager()
    ok = sm.data.get("x") == "🔴 Unpaid" and "_commitments" in sm.data
    check(f"state read: {label}", ok)
    os.unlink(tmp)

print("\n=== 6. Cache layer (cache.py) + state _calendars map ===")
from pm_calendar_sync import cache as pkg_cache

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "sub" / "test.json"
    payload = {"refreshed_at": "2026-07-02T00:00:00",
               "rows": [{"occupancy_id": 1, "tenant": "Dôe, Jañe 🔴"}]}
    pkg_cache.save_json(p, payload)
    check("cache round-trip (creates parent dirs)", pkg_cache.load_json(p) == payload)
    check("atomic write leaves no .tmp behind", not p.with_suffix(".tmp").exists())
    check("missing file -> None", pkg_cache.load_json(Path(td) / "nope.json") is None)
    bad = Path(td) / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    check("corrupt file -> None", pkg_cache.load_json(bad) is None)
    lst = Path(td) / "list.json"
    lst.write_text("[1, 2]", encoding="utf-8")
    check("non-dict payload -> None", pkg_cache.load_json(lst) is None)
    bom = Path(td) / "bom.json"
    bom.write_text(json.dumps(payload), encoding="utf-16")
    check("utf-16 BOM cache read", pkg_cache.load_json(bom) == payload)

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({"_commitments": {}}, f)
    tmp = f.name
with mock.patch.object(state, "STATE_FILE", Path(tmp)):
    sm = state.StateManager()
check("_calendars auto-created on load", sm.data.get("_calendars") == {})
sm.set_calendar_id(42, "cal_abc")
check("get/set_calendar_id round-trip (int key coerced)",
      sm.get_calendar_id("42") == "cal_abc")
os.unlink(tmp)

print("\n=== 7. RUN_MODE entrypoint dispatch (sync.py / python -m) ===")
import pm_calendar_sync.__main__ as entry

calls = []
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(entry.SyncOrchestrator, "run",
                       lambda self, mode="full_nightly": calls.append(("run", mode))), \
     mock.patch.object(entry.SyncOrchestrator, "run_update",
                       lambda self: calls.append(("run_update",)), create=True), \
     mock.patch.object(entry.SyncOrchestrator, "run_submit",
                       lambda self: calls.append(("run_submit",)), create=True):
    for rm in ("full_nightly", "full", "update", "submit", "bogus"):
        os.environ["RUN_MODE"] = rm
        entry.main()
    os.environ.pop("RUN_MODE", None)
check("full_nightly -> run('full_nightly')", calls[0] == ("run", "full_nightly"))
check("full -> run('full')", calls[1] == ("run", "full"))
check("update -> run_update", calls[2] == ("run_update",))
check("submit -> run_submit", calls[3] == ("run_submit",))
check("unknown mode falls back to run('full')", calls[4] == ("run", "full"))

print()
if failures:
    print(f"❌ {len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("✅ All smoke-test checks passed. Package is wired correctly and all four fixes behave.")
