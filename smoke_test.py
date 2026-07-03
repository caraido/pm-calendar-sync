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

print("\n=== 8. update-mode money diff (diff_rent_roll) ===")
check("run_update method exists", hasattr(orch, "run_update"))
cached = [
    {"occupancy_id": 1, "status": "Current", "past_due": 100.0, "rent": 950},
    {"occupancy_id": 2, "status": "Current", "past_due": "0",   "rent": 1200},
    {"occupancy_id": 3, "status": "Current", "past_due": None,  "rent": 800},
    {"occupancy_id": 9, "status": "Notice",  "past_due": 50.0,  "rent": 700},
]
fresh = [
    {"occupancy_id": 1, "status": "Current", "past_due": 100.004, "rent": 950},   # within eps
    {"occupancy_id": 2, "status": "Current", "past_due": 600.0,   "rent": 1200},  # money moved
    {"occupancy_id": 3, "status": "Current", "past_due": 0,       "rent": 850},   # rent changed
    {"occupancy_id": 4, "status": "Current", "past_due": 0.0,     "rent": 1000},  # new lease
    {"occupancy_id": 9, "status": "Notice",  "past_due": 999.0,   "rent": 700},   # non-Current
]
d = transforms.diff_rent_roll(cached, fresh)
check("delta within eps ignored", "1" not in d)
check("past_due change flagged", "2" in d)
check("rent change flagged", "3" in d)
check("new lease flagged", "4" in d)
check("non-Current rows ignored", "9" not in d)
check("no snapshot -> all Current oids",
      transforms.diff_rent_roll(None, fresh) == {"1", "2", "3", "4"})
check("None/str value coercion (None == 0 == '0')",
      transforms.diff_rent_roll(
          [{"occupancy_id": 3, "status": "Current", "past_due": None, "rent": "800"}],
          [{"occupancy_id": 3, "status": "Current", "past_due": "0",  "rent": 800.0}],
      ) == set())

print("\n=== 9. E-a: shared drag-detection primitives (pre-listed index) ===")
from datetime import date as _date

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({"_commitments": {}}, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch2 = SyncOrchestrator()
os.unlink(tmp)

idx = {"ev1": {"id": "ev1", "start": {"date": "2026-08-05"}},
       "ev2": {"id": "ev2", "start": {"dateTime": "2026-08-06T10:00:00-05:00"}}}
check("_live_event_start: index date",
      orch2._live_event_start("cal", "ev1", idx) == "2026-08-05")
check("_live_event_start: index dateTime",
      orch2._live_event_start("cal", "ev2", idx) == "2026-08-06")
check("_live_event_start: missing-from-index == gone",
      orch2._live_event_start("cal", "nope", idx) is None)
with mock.patch.object(orch2.gcal, "get_event_start_date",
                       return_value="2026-09-01") as ges:
    ok = orch2._live_event_start("cal", "ev1") == "2026-09-01"
    check("_live_event_start: no index -> live GET", ok and ges.called)

with mock.patch.object(orch2.gcal, "find_all_events_by_type") as faebt:
    orch2._process_commitments(
        "69@10", "cal", {"past_due": 0.0, "rent": 900.0},
        _date(2026, 7, 2), has_known_or_new=True, events=[])
    check("_process_commitments(events=[]) skips the live list call",
          not faebt.called)
with mock.patch.object(orch2.gcal, "find_all_events_by_type",
                       return_value=[]) as faebt:
    orch2._process_commitments(
        "69@10", "cal", {"past_due": 0.0, "rent": 900.0},
        _date(2026, 7, 2), has_known_or_new=True)
    check("_process_commitments() still lists live by default", faebt.called)

# End-to-end conversion through the index: a dragged status event becomes a
# registered commitment without any live GET.
months = set()
unit_stub = {"occupancy_id": "69", "rent": 900.0, "past_due": 900.0}
with mock.patch.object(orch2.gcal, "convert_to_commitment") as conv, \
     mock.patch.object(orch2.gcal, "get_event_start_date") as ges:
    hit = orch2._convert_status_drag(
        "69@10", "cal", unit_stub, "ev1", "2026-07-01",
        _date(2026, 7, 2), "2026-07", months, "🔴 Unpaid",
        events_by_id={"ev1": {"id": "ev1", "start": {"date": "2026-07-20"}}})
    check("_convert_status_drag: converts via index (no live GET)",
          hit and conv.called and not ges.called and "2026-07" in months)
    check("_convert_status_drag: promise registered in state",
          any(c["event_id"] == "ev1"
              for c in orch2.state.get_commitments("69@10")))
    hit2 = orch2._convert_status_drag(
        "69@10", "cal", unit_stub, "ev9", "2026-07-01",
        _date(2026, 7, 2), "2026-07", set(), "🔴 Unpaid",
        events_by_id={"ev9": {"id": "ev9", "start": {"date": "2026-07-01"}}})
    check("_convert_status_drag: unmoved event -> no conversion", not hit2)

print("\n=== 10. E-b: list_all_events grouping + submit-mode unit flow ===")
check("run_submit method exists", hasattr(orch, "run_submit"))

# list_all_events: pagination + grouping by okpm_occupancy_id, untagged skipped
pages = [
    {"items": [
        {"id": "a1", "extendedProperties": {"private": {
            "okpm_occupancy_id": "69", "okpm_event_type": "status"}}},
        {"id": "b1", "extendedProperties": {"private": {
            "okpm_occupancy_id": "70", "okpm_event_type": "commitment"}}},
        {"id": "x1"},  # untagged — skipped
    ], "nextPageToken": "t"},
    {"items": [
        {"id": "a2", "extendedProperties": {"private": {
            "okpm_occupancy_id": "69", "okpm_event_type": "rent"}}},
    ]},
]
orch2.gcal.service.events.return_value.list.return_value.execute.side_effect = pages
grouped = orch2.gcal.list_all_events("cal")
check("list_all_events groups by oid across pages",
      [e["id"] for e in grouped.get("69", [])] == ["a1", "a2"]
      and [e["id"] for e in grouped.get("70", [])] == ["b1"])
check("list_all_events skips untagged events",
      all("x1" not in [e["id"] for e in evs] for evs in grouped.values()))

# Submit-mode unit flow: dragged status event -> in-place conversion; the
# fresh commitment is patched into the pre-listed snapshot (NOT treated as
# PM-deleted); the promised month's placeholder is absorbed.
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({
        "_commitments": {},
        "77@5_2026-07": {"status": "🔴 Unpaid", "past_due": 1000.0,
                         "calendar_id": "cal", "status_event_id": "st77",
                         "status_event_date": "2026-07-01",
                         "late_event_id": None,
                         "payment_event_ids": [], "payment_event_dates": []},
        "77@5_2026-08": {"status": "🔴 Unpaid", "past_due": 0.0,
                         "calendar_id": "cal", "rent_event_id": "ph8",
                         "late_event_id": None},
    }, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch3 = SyncOrchestrator()
os.unlink(tmp)

unit77 = {"occupancy_id": "77", "rent": 1000.0, "past_due": 1000.0}
listed = [  # the pre-run listing: dragged status event + untouched placeholder
    {"id": "st77", "start": {"date": "2026-08-15"},
     "extendedProperties": {"private": {
         "okpm_occupancy_id": "77", "okpm_event_type": "status"}}},
    {"id": "ph8", "start": {"date": "2026-08-01"},
     "extendedProperties": {"private": {
         "okpm_occupancy_id": "77", "okpm_event_type": "rent"}}},
]
converted_body = {  # what convert_to_commitment writes server-side
    "summary": "🔴 promise", "start": {"date": "2026-08-15"},
    "end": {"date": "2026-08-16"},
    "description": "PROMISED: [fill in]\n────\nauto",
    "extendedProperties": {"private": {
        "okpm_occupancy_id": "77", "okpm_event_type": "commitment",
        "okpm_source_type": "status"}},
}
_today = _date(2026, 7, 2)
with mock.patch.object(orch3.gcal, "convert_to_commitment",
                       return_value=converted_body) as conv, \
     mock.patch.object(orch3.gcal, "update_commitment_event",
                       return_value="2026-08-15") as upd, \
     mock.patch.object(orch3.gcal, "delete_event") as dele, \
     mock.patch.object(orch3.gcal, "find_all_events_by_type") as faebt:
    orch3._detect_and_convert_drags(
        "77@5", "cal", unit77, _today, "2026-07", listed)
    orch3._process_commitments(
        "77@5", "cal", unit77, _today, has_known_or_new=True,
        events=orch3._commitment_events_for("77@5", listed))
    orch3._absorb_promised_placeholders("77@5", "cal", _today)

check("submit: dragged status event converted in place", conv.called)
check("submit: month entry persisted with status_event_id=None",
      orch3.state.get("77@5", "2026-07").get("status_event_id") is None)
comms = orch3.state.get_commitments("77@5")
check("submit: promise registered (anchor 2026-08-15, covers 2026-07)",
      len(comms) == 1 and comms[0]["event_id"] == "st77"
      and comms[0]["anchor_date"] == "2026-08-15"
      and comms[0]["covers_rent_month"] == "2026-07")
check("submit: fresh commitment consolidated, not treated as PM-deleted",
      upd.called
      and orch3.gcal.service.events.return_value.insert.call_count == 0)
check("submit: no live listing needed (pre-listed events used)",
      not faebt.called)
check("submit: promised month's placeholder absorbed",
      dele.call_args == mock.call("cal", "ph8")
      and orch3.state.get("77@5", "2026-08").get("rent_event_id") is None)

print()
if failures:
    print(f"❌ {len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("✅ All smoke-test checks passed. Package is wired correctly and all four fixes behave.")
