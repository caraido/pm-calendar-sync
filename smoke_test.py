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

print()
if failures:
    print(f"❌ {len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("✅ All smoke-test checks passed. Package is wired correctly and all four fixes behave.")
