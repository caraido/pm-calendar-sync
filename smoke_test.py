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
os.environ.setdefault("PM_EMAIL", "pm@example.com")

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
check("_retired_calendars/_migrations auto-created on load",
      sm.data.get("_retired_calendars") == {} and sm.data.get("_migrations") == {})
sm.set_calendar_id(42, "cal_abc")
check("get/set_calendar_id round-trip (int key coerced)",
      sm.get_calendar_id("42") == "cal_abc")
sm.set_calendar_id("g3", "cal_g3")
check("group scope-key ('g3') round-trip", sm.get_calendar_id("g3") == "cal_g3")
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
    {"occupancy_id": 9, "status": "Notice-Unrented", "past_due": 50.0, "rent": 700},
]
fresh = [
    {"occupancy_id": 1, "status": "Current", "past_due": 100.004, "rent": 950},   # within eps
    {"occupancy_id": 2, "status": "Current", "past_due": 600.0,   "rent": 1200},  # money moved
    {"occupancy_id": 3, "status": "Current", "past_due": 0,       "rent": 850},   # rent changed
    {"occupancy_id": 4, "status": "Current", "past_due": 0.0,     "rent": 1000},  # new lease
    {"occupancy_id": 9, "status": "Notice-Unrented", "past_due": 999.0, "rent": 700},  # Notice: money moved
    {"occupancy_id": None, "status": "Vacant-Unrented", "past_due": None, "rent": None},  # no occupancy
]
d = transforms.diff_rent_roll(cached, fresh)
check("delta within eps ignored", "1" not in d)
check("past_due change flagged", "2" in d)
check("rent change flagged", "3" in d)
check("new lease flagged", "4" in d)
check("Notice row with occupancy_id diffed (money moved)", "9" in d)
check("row without occupancy_id ignored", "None" not in d)
check("no snapshot -> all oids with an occupancy",
      transforms.diff_rent_roll(None, fresh) == {"1", "2", "3", "4", "9"})
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

# list_all_events: pagination + grouping by okpm_occupancy_id; untagged
# events come back in their own list (adoption-scan input), never grouped
pages = [
    {"items": [
        {"id": "a1", "extendedProperties": {"private": {
            "okpm_occupancy_id": "69", "okpm_event_type": "status"}}},
        {"id": "b1", "extendedProperties": {"private": {
            "okpm_occupancy_id": "70", "okpm_event_type": "commitment"}}},
        {"id": "x1"},  # untagged — returned separately
    ], "nextPageToken": "t"},
    {"items": [
        {"id": "a2", "extendedProperties": {"private": {
            "okpm_occupancy_id": "69", "okpm_event_type": "rent"}}},
    ]},
]
orch2.gcal.service.events.return_value.list.return_value.execute.side_effect = pages
grouped, untagged_evs = orch2.gcal.list_all_events("cal")
check("list_all_events groups by oid across pages",
      [e["id"] for e in grouped.get("69", [])] == ["a1", "a2"]
      and [e["id"] for e in grouped.get("70", [])] == ["b1"])
check("list_all_events returns untagged events separately",
      [e["id"] for e in untagged_evs] == ["x1"]
      and all("x1" not in [e["id"] for e in evs] for evs in grouped.values()))

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

print("\n=== 11. Bug-1 fix: commitment auto-section parser ===")
unit_fx = {
    "occupancy_id": "99", "property_name": "31 West 112th Place",
    "address": "31 West 112Th Place, Chicago, IL, 60628", "unit_label": "Unit 2",
    "tenant": "Burdine, Tyquita", "additional_tenants": "",
    "rent": 1400.0, "past_due": 700.0, "amount_paid": 0.0, "payments": [],
    "phone": "N/A", "late_fee_desc": "N/A", "grace_days": 5,
    "lease_from": "2025-11-15", "lease_to": "2026-11-30",
}
for src in ("status", "payment", "kickstart", "late"):
    body = orch.gcal._build_commitment_event(
        unit_fx, "2026-07-03", src, 700.0, pm_notes="PROMISED: $700")
    parsed = transforms.parse_commitment_auto_section(body["description"])
    check(f"parser round-trips source_type {src!r}",
          parsed is not None and parsed["source_type"] == src
          and parsed["tenant"] == "Tyquita Burdine"
          and parsed["pm_notes"] == "PROMISED: $700")
check("no divider -> None",
      transforms.parse_commitment_auto_section("hello world") is None)
mangled = body["description"].replace("Source:", "Sauce:")
check("mangled Source line -> default 'status'",
      transforms.parse_commitment_auto_section(mangled)["source_type"] == "status")
no_tenant = body["description"].replace("Tenant:", "Tenannt:")
check("missing Tenant line -> tenant None",
      transforms.parse_commitment_auto_section(no_tenant)["tenant"] is None)

print("\n=== 12. Bug-1 fix: adoption of untagged PM copies ===")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({"_commitments": {}}, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch4 = SyncOrchestrator()
os.unlink(tmp)

# The scan's client-side filter: tagged events excluded, everything
# untagged returned (classification is the adopter's job now).
divider_desc = orch4.gcal._build_commitment_event(
    unit_fx, "2026-07-03", "status", 1400.0,
    pm_notes="PROMISED: full balance")["description"]
pages = [{"items": [
    {"id": "t1", "description": divider_desc,
     "extendedProperties": {"private": {"okpm_occupancy_id": "99"}}},
    {"id": "u1", "description": divider_desc, "start": {"date": "2026-07-03"}},
    {"id": "u2", "description": "AUTO-SYNCED mention but no divider"},
]}]
orch4.gcal.service.events.return_value.list.return_value.execute.side_effect = pages
found = orch4.gcal.find_untagged_sync_candidates("calA")
check("scan keeps only untagged events",
      [e["id"] for e in found] == ["u1", "u2"])
check("classifier: divider copy -> commitment kind",
      transforms.classify_sync_copy("", divider_desc)["kind"] == "commitment")
check("classifier: non-sync text -> ignored",
      transforms.classify_sync_copy("", "AUTO-SYNCED mention but no divider")
      is None)

row_fx = {"occupancy_id": 99, "tenant": "Burdine, Tyquita",
          "additional_tenants": "", "rent": "1400.00", "past_due": "700.00",
          "status": "Current", "property_name": "31 West 112th Place",
          "unit": "2", "property_street": "31 West 112Th Place",
          "property_city": "Chicago", "property_state": "IL",
          "property_zip": "60628", "lease_from": "2025-11-15",
          "lease_to": "2026-11-30"}
copy_ev = {"id": "copy1", "start": {"date": "2026-07-03"},
           "description": divider_desc}   # no extendedProperties (UI copy)
upd = orch4.gcal.service.events.return_value.update
with mock.patch.object(orch4.gcal, "find_untagged_sync_candidates",
                       return_value=[copy_ev]), \
     mock.patch.object(orch4, "_mirror_commitment_to_siblings") as mir:
    n = orch4._adopt_untagged_copies(
        [(row_fx, {"group_id": 9, "group_name": "Bowei Yan"})], "g9",
        "calA", {}, {}, _date(2026, 7, 4))
body_sent = upd.call_args.kwargs["body"]
check("adoption re-tags the event in place",
      n == 1 and upd.called
      and body_sent["extendedProperties"]["private"]["okpm_event_type"] == "commitment"
      and body_sent["extendedProperties"]["private"]["okpm_occupancy_id"] == "99")
comms = orch4.state.get_commitments("99@g9")
check("adoption registers the promise (covers current month)",
      len(comms) == 1 and comms[0]["event_id"] == "copy1"
      and comms[0]["anchor_date"] == "2026-07-03"
      and comms[0]["covers_rent_month"] == "2026-07")
check("adoption patches _fresh_commitments and mirrors",
      "copy1" in orch4._fresh_commitments and mir.called)
check("PM notes preserved through adoption",
      body_sent["description"].startswith("PROMISED: full balance"))

upd.reset_mock()
other_row = {**row_fx, "occupancy_id": 98, "tenant": "Else, Someone"}
with mock.patch.object(orch4.gcal, "find_untagged_sync_candidates",
                       return_value=[copy_ev]):
    n2 = orch4._adopt_untagged_copies(
        [(other_row, {})], "g9", "calA", {}, {}, _date(2026, 7, 4))
check("unmatched tenant -> skipped, no writes", n2 == 0 and not upd.called)

twin = {**row_fx, "occupancy_id": 98}   # same tenant, same address/unit
with mock.patch.object(orch4.gcal, "find_untagged_sync_candidates",
                       return_value=[copy_ev]):
    n3 = orch4._adopt_untagged_copies(
        [(row_fx, {}), (twin, {})], "g9", "calA", {}, {}, _date(2026, 7, 4))
check("ambiguous tenant match -> skipped, no writes",
      n3 == 0 and not upd.called)

print("\n=== 13. Bug-2 fix: build_reversal_map ===")
ledger_fx = [
    {"payer": "Darden, Sanquia", "credit": "-1530.00", "date": "2026-07-01",
     "description": "NSF reversal receipt for Reference #1A4A-5A70"},
    {"payer": "Doe, Jon", "credit": "-25.00", "date": "2026-07-02",
     "description": "Balance adjustment"},
    {"payer": "Doe, Jon", "credit": "500.00", "date": "2026-07-02",
     "description": "ACH Payment (Reference #AAAA-BBBB)"},
    {"payer": "X", "credit": None, "date": "2026-07-03", "description": "noise"},
]
rmap = transforms.build_reversal_map(ledger_fx)
check("negative row with ref parsed",
      rmap.get("Sanquia Darden") == [{
          "date": "2026-07-01", "amount": 1530.0, "ref": "1A4A-5A70",
          "description": "NSF reversal receipt for Reference #1A4A-5A70"}])
check("negative row without ref -> ref None",
      rmap.get("Jon Doe") and rmap["Jon Doe"][0]["ref"] is None
      and rmap["Jon Doe"][0]["amount"] == 25.0)
check("positive / null rows excluded", len(rmap.get("Jon Doe", [])) == 1
      and "X" not in rmap)
pmap = transforms.build_payment_map(ledger_fx)
check("payment_map unchanged (positives only, keyword is_nsf intact)",
      list(pmap) == ["Jon Doe"] and pmap["Jon Doe"][0]["amount"] == 500.0)

print("\n=== 14. Bug-2 fix: first-payment NSF renders red ===")
nsf_pay = {"date": "2026-06-04", "amount": 1400.0, "is_nsf": True,
           "description": "ACH (#REF1)", "intended_month": None}
b = orch.gcal._build_status_event(unit_fx, status.STATUS_PARTIAL,
                                  _date(2026, 6, 4), nsf_pay, 1400.0,
                                  total_payments=1)
check("NSF first payment -> red + NSF tag",
      b["colorId"] == "11" and b["summary"].startswith("🔴")
      and " NSF" in b["summary"])
b2 = orch.gcal._build_status_event(unit_fx, status.STATUS_PARTIAL,
                                   _date(2026, 6, 4), nsf_pay, 0.0,
                                   total_payments=2)
check("NSF first payment stays red (grey muting removed — settled months "
      "collapse instead)", b2["colorId"] == "11")
ok_pay = {**nsf_pay, "is_nsf": False}
b3 = orch.gcal._build_status_event(unit_fx, status.STATUS_PARTIAL,
                                   _date(2026, 6, 4), ok_pay, 700.0,
                                   total_payments=1)
check("non-NSF first payment unchanged (yellow)", b3["colorId"] == "5")
b4 = orch.gcal._build_status_event(
    unit_fx, status.STATUS_UNPAID, _date(2026, 7, 1),
    reversal_notes=["⚠️ $1,530.00 payment REVERSED (NSF) on Jul 01, 2026"])
check("reversal notes rendered on the status event",
      "REVERSED (NSF) on Jul 01, 2026" in b4["description"])

print("\n=== 15. Bug-2 fix: prior-month NSF reconciliation ===")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({
        "_commitments": {},
        "85@10_2026-06": {"status": "✅ Paid", "past_due": 0.0,
                          "calendar_id": "calA", "status_event_id": "st6",
                          "status_event_date": "2026-06-05",
                          "late_event_id": None,
                          "payment_event_ids": ["p61", "p62"]},
        "85@10_2026-07": {"status": "🔴 Unpaid", "past_due": 3030.0,
                          "calendar_id": "calA", "status_event_id": "st7",
                          "status_event_date": "2026-07-01",
                          "late_event_id": None, "payment_event_ids": []},
    }, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch5 = SyncOrchestrator()
os.unlink(tmp)

bodies = {
    "st6": {"id": "st6", "colorId": "8",
            "summary": "⚪ · Sanquia Darden · Unit 2 · 7736 South Greenwood Avenue · $500 paid",
            "description": "Payment 1 of 3\nStatus:       🟡 Partial"},
    "p61": {"id": "p61", "colorId": "8",
            "summary": "⚪ · Sanquia Darden · Unit 2 · 7736 South Greenwood Avenue · $500",
            "description": "Method:       ACH (#OTHER-REF)\nAmount:       $500.00\nStatus:       🟡 Partial"},
    "p62": {"id": "p62", "colorId": "2",
            "summary": "✅ · Sanquia Darden · Unit 2 · 7736 South Greenwood Avenue · $1,530",
            "description": ("Method:       ACH (#1A4A-5A70)\n"
                            "Amount:       $1,530.00\n"
                            "Received in June: $1,530.00\n"
                            "Balance after this payment: $0.00\n"
                            "Status:       ✅ Paid")},
}
unit85 = {"occupancy_id": "85", "tenant": "Darden, Sanquia",
          "additional_tenants": "", "rent": 1500.0, "past_due": 3030.0,
          "property_name": "7736 South Greenwood Avenue", "unit_label": "Unit 2",
          "address": "7736 South Greenwood Avenue, Chicago, IL, 60619",
          "phone": "N/A", "late_fee_desc": "N/A", "grace_days": 5,
          "amount_paid": 0.0, "payments": [],
          "lease_from": "2026-01-01", "lease_to": "2026-12-31"}
rmap85 = {"Sanquia Darden": [
    {"date": "2026-07-01", "amount": 1530.0, "ref": "1A4A-5A70",
     "description": "NSF reversal receipt for Reference #1A4A-5A70"},
    {"date": "2026-07-01", "amount": 99.0, "ref": "1A4A",
     "description": "bogus prefix ref must not match"},
]}
upd5 = orch5.gcal.service.events.return_value.update
with mock.patch.object(orch5.gcal, "get_event",
                       side_effect=lambda cal, eid: bodies.get(eid)):
    orch5._apply_nsf_reversals("85@10", "calA", unit85, _date(2026, 7, 4),
                               "2026-07", rmap85,
                               surplus_payment_ids=[], prior_payments=[])
    first_pass_updates = upd5.call_count
    check("reversed payment event flipped red + NSF + note",
          bodies["p62"]["colorId"] == "11"
          and bodies["p62"]["summary"].startswith("🔴")
          and " NSF" in bodies["p62"]["summary"]
          and "REVERSED (NSF)" in bodies["p62"]["description"])
    check("flipped event's Status line rewritten (no stale 'Paid')",
          "Status:       🔴 REVERSED / NSF" in bodies["p62"]["description"]
          and "✅ Paid" not in bodies["p62"]["description"])
    check("historical dollar lines stamped '(before reversal)'",
          "Balance after this payment: $0.00  (before reversal)"
          in bodies["p62"]["description"]
          and "Received in June: $1,530.00  (before reversal)"
          in bodies["p62"]["description"]
          and "Amount:       $1,530.00\n" in bodies["p62"]["description"])
    check("month's muted events un-greyed to their own status",
          bodies["p61"]["colorId"] == "5" and bodies["st6"]["colorId"] == "5")
    check("status event notes the broken settlement",
          "month no longer settled" in bodies["st6"]["description"])
    june = orch5.state.get("85@10", "2026-06")
    check("idempotence marker recorded on the June entry",
          [r["key"] for r in june["nsf_reversals_applied"]] == ["1A4A-5A70"])
    check("prefix ref '#1A4A' did NOT match '#1A4A-5A70'",
          all(r["key"] != "1A4A" for r in june["nsf_reversals_applied"]))
    orch5._apply_nsf_reversals("85@10", "calA", unit85, _date(2026, 7, 4),
                               "2026-07", rmap85,
                               surplus_payment_ids=[], prior_payments=[])
    check("second pass is a no-op (markers honored)",
          upd5.call_count == first_pass_updates)

print("\n=== 16. Bug-2 fix: current-month vanished-row handling ===")
bodies7 = {
    "p71": {"id": "p71", "colorId": "2",
            "summary": "✅ · Sanquia Darden · Unit 2 · 7736 South Greenwood Avenue · $800",
            "description": "Method:       ACH (#CCCC-DDDD)\nAmount:       $800.00\nStatus:       ✅ Paid",
            "extendedProperties": {"private": {"okpm_payment_idx": "1"}}},
    "p72": {"id": "p72", "colorId": "2",
            "summary": "✅ · Sanquia Darden · Unit 2 · 7736 South Greenwood Avenue · $300",
            "description": "Method:       ACH (#GGGG-HHHH)\nAmount:       $300.00\nStatus:       ✅ Paid"},
    "st7": {"id": "st7", "colorId": "11",
            "summary": "🔴 · Sanquia Darden · Unit 2 · 7736 South Greenwood Avenue · $3,030 due",
            "description": "No payments received yet.\nStatus:       🔴 Unpaid"},
}
rmap7 = {"Sanquia Darden": [
    {"date": "2026-07-02", "amount": 800.0, "ref": "CCCC-DDDD",
     "description": "NSF reversal receipt for Reference #CCCC-DDDD"},
    {"date": "2026-07-02", "amount": 650.0, "ref": "EEEE-FFFF",
     "description": "NSF reversal receipt for Reference #EEEE-FFFF"},
    {"date": "2026-04-01", "amount": 111.0, "ref": "OLD1-OLD1",
     "description": "NSF reversal receipt for Reference #OLD1-OLD1"},
]}
prior_pays = [{"date": "2026-07-02", "amount": 650.0, "is_nsf": False,
               "description": "ACH (#EEEE-FFFF)"}]
orch5.gcal.service.events.return_value.insert.return_value.execute.return_value = \
    {"id": "ghost1"}
with mock.patch.object(orch5.gcal, "get_event",
                       side_effect=lambda cal, eid: bodies7.get(eid)), \
     mock.patch.object(orch5.gcal, "delete_event") as dele5:
    orch5._apply_nsf_reversals("85@10", "calA", unit85, _date(2026, 7, 4),
                               "2026-07", rmap7,
                               surplus_payment_ids=["p71", "p72"],
                               prior_payments=prior_pays)
check("matching surplus event flipped + idx retagged",
      bodies7["p71"]["colorId"] == "11"
      and bodies7["p71"]["extendedProperties"]["private"]["okpm_payment_idx"] == "nsf1"
      and bodies7["p71"]["extendedProperties"]["private"]["okpm_nsf"] == "1"
      and "Status:       🔴 REVERSED / NSF" in bodies7["p71"]["description"])
check("non-matching surplus event deleted (positional duplicate)",
      dele5.call_args == mock.call("calA", "p72"))
july = orch5.state.get("85@10", "2026-07")
check("flipped event recorded in nsf_event_ids + marker",
      "p71" in (july.get("nsf_event_ids") or [])
      and "CCCC-DDDD" in [r["key"] for r in july["nsf_reversals_applied"]])
check("vanished single payment matched via stored payments -> noted",
      "EEEE-FFFF" in [r["key"] for r in july["nsf_reversals_applied"]]
      and "REVERSED (NSF)" in bodies7["st7"]["description"])
ghost_body = orch5.gcal.service.events.return_value.insert.call_args.kwargs["body"]
check("vanished payment reconstructed as red ghost event (idx nsfg, tracked)",
      ghost_body["colorId"] == "11"
      and ghost_body["extendedProperties"]["private"]["okpm_payment_idx"] == "nsfg"
      and "$650.00" in ghost_body["description"]
      and "Reconstructed from sync records" in ghost_body["description"]
      and "Balance" not in ghost_body["description"]
      and "ghost1" in (july.get("nsf_event_ids") or []))
# The mocked discovery.build was captured at import, so every orchestrator
# shares ONE service mock — clear the insert counter for later sections
# that assert exact insert counts (e.g. the cutover suite).
orch5.gcal.service.events.return_value.insert.reset_mock()
check("62-day-old reversal ignored (no marker)",
      "OLD1-OLD1" not in [r["key"] for r in july["nsf_reversals_applied"]])
check("new markers carry v=2",
      all(r.get("v") == 2 for r in july["nsf_reversals_applied"]))

print("\n=== 17. Bug-2 fix: legacy flipped events get a one-time retouch ===")
# Simulates events flipped by the previous code version: already red + NSF
# tag + note, but Status still '✅ Paid' and the marker lacking v=2.
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({
        "_commitments": {},
        "85@10_2026-06": {"status": "✅ Paid", "past_due": 0.0,
                          "calendar_id": "calA", "status_event_id": "q6s",
                          "status_event_date": "2026-06-05",
                          "late_event_id": None,
                          "payment_event_ids": ["q62"],
                          "nsf_reversals_applied": [
                              {"key": "1A4A-5A70", "ref": "1A4A-5A70",
                               "date": "2026-07-01", "amount": 1530.0}]},
        "85@10_2026-07": {"status": "🔴 Unpaid", "past_due": 3030.0,
                          "calendar_id": "calA", "status_event_id": "st7b",
                          "status_event_date": "2026-07-01",
                          "late_event_id": None, "payment_event_ids": []},
    }, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch6 = SyncOrchestrator()
os.unlink(tmp)

note26 = "⚠️ $1,530.00 payment REVERSED (NSF) on Jul 01, 2026"
bodies8 = {
    "q62": {"id": "q62", "colorId": "11",
            "summary": "🔴 · Sanquia Darden · Unit 2 · 7736 South Greenwood Avenue · $1,530 NSF",
            "description": ("Method:       ACH (#1A4A-5A70)\n"
                            "Amount:       $1,530.00\n"
                            "Balance after this payment: $0.00\n"
                            "Status:       ✅ Paid\n" + note26)},
    "q6s": {"id": "q6s", "colorId": "5",
            "summary": "🟡 · Sanquia Darden · Unit 2 · 7736 South Greenwood Avenue · $500 paid",
            "description": ("Status:       🟡 Partial\n"
                            + note26 + " — month no longer settled")},
}
upd6 = orch6.gcal.service.events.return_value.update
rmap8 = {"Sanquia Darden": [
    {"date": "2026-07-01", "amount": 1530.0, "ref": "1A4A-5A70",
     "description": "NSF reversal receipt for Reference #1A4A-5A70"}]}
with mock.patch.object(orch6.gcal, "get_event",
                       side_effect=lambda cal, eid: bodies8.get(eid)):
    orch6._apply_nsf_reversals("85@10", "calA",
                               {"occupancy_id": "85",
                                "tenant": "Darden, Sanquia",
                                "additional_tenants": "",
                                "rent": 1500.0, "past_due": 3030.0},
                               _date(2026, 7, 5), "2026-07", rmap8,
                               surplus_payment_ids=[], prior_payments=[])
    retouch_updates = upd6.call_count
    check("legacy flip retouched: Status rewritten, lines stamped",
          "Status:       🔴 REVERSED / NSF" in bodies8["q62"]["description"]
          and "Balance after this payment: $0.00  (before reversal)"
          in bodies8["q62"]["description"])
    june6 = orch6.state.get("85@10", "2026-06")
    check("legacy marker upgraded to v=2 without duplication",
          len(june6["nsf_reversals_applied"]) == 1
          and june6["nsf_reversals_applied"][0]["v"] == 2)
    orch6._apply_nsf_reversals("85@10", "calA",
                               {"occupancy_id": "85",
                                "tenant": "Darden, Sanquia",
                                "additional_tenants": "",
                                "rent": 1500.0, "past_due": 3030.0},
                               _date(2026, 7, 5), "2026-07", rmap8,
                               surplus_payment_ids=[], prior_payments=[])
    check("retouch happens exactly once (v=2 honored)",
          upd6.call_count == retouch_updates)

print("\n=== 18. Group cutover: build_group_property_map / scope keys ===")
grp_rows = [
    {"property_group_id": 8, "property_group_name": "L&P Midwest Capital",
     "property_id": 24},
    {"property_group_id": 8, "property_group_name": "L&P Midwest Capital",
     "property_id": 40},
    {"property_group_id": 3, "property_group_name": "Ryan Palmer Property Group",
     "property_id": 40},
    {"property_group_id": 8, "property_group_name": "L&P Midwest Capital",
     "property_id": 24},   # duplicate membership row
    {"property_group_id": None,
     "property_group_name": "Properties not assigned to a property group",
     "property_id": 47},
    {"property_group_id": 9, "property_group_name": "Bowei Yan",
     "property_id": "42"},  # str property_id
]
gm = transforms.build_group_property_map(grp_rows)
check("null group skipped (unassigned properties unsynced)", 47 not in gm)
check("multi-group property yields one entry per group",
      [g["group_id"] for g in gm[40]] == [8, 3])
check("duplicate membership rows deduped", len(gm[24]) == 1)
check("str property_id coerced to int key", 42 in gm)
check("scope key format g{id}",
      transforms.group_scope_key(3) == "g3"
      and transforms.group_scope_key(" 9 ") == "g9")
check("group display name verbatim / fallback",
      transforms.group_display_name({"group_name": "Bowei Yan"}) == "Bowei Yan"
      and transforms.group_display_name({}) == "Unknown Group")

print("\n=== 19. Group calendar creation (verbatim summary, no ' Portfolio') ===")
svc19 = orch2.gcal.service
svc19.calendarList.return_value.list.return_value.execute.side_effect = None
svc19.calendarList.return_value.list.return_value.execute.return_value = {"items": []}
svc19.calendars.return_value.insert.return_value.execute.return_value = {"id": "gc_new"}
cid19 = orch2.gcal.get_or_create_group_calendar("L&P Midwest Capital")
ins19_body = svc19.calendars.return_value.insert.call_args.kwargs["body"]
check("created id returned + cached (2nd call hits cache)",
      cid19 == "gc_new"
      and orch2.gcal.get_or_create_group_calendar("L&P Midwest Capital") == "gc_new")
check("summary is the group name VERBATIM",
      ins19_body["summary"] == "L&P Midwest Capital")
check("new calendar recorded for ACL bootstrap",
      "gc_new" in orch2.gcal.created_calendar_ids)

# retire_calendar: rename + selective ACL strip.  Must skip the PM, service
# accounts, non-user scopes, and the calendar's own primary-owner pseudo-rule
# (scope value == calendar id — deleting it 403s with cannotChangeOwnerAcl).
svc19.calendars.return_value.get.return_value.execute.return_value = {
    "summary": "Xin, Tian Portfolio"}
svc19.acl.return_value.list.return_value.execute.return_value = {"items": [
    {"id": "r1", "role": "owner",
     "scope": {"type": "user", "value": "oldcal19@group.calendar.google.com"}},
    {"id": "r2", "role": "owner",
     "scope": {"type": "user", "value": "pm@example.com"}},
    {"id": "r3", "role": "writer",
     "scope": {"type": "user", "value": "bot@proj.iam.gserviceaccount.com"}},
    {"id": "r4", "role": "reader",
     "scope": {"type": "user", "value": "owner.person@gmail.com"}},
    {"id": "r5", "role": "reader", "scope": {"type": "default"}},
]}
ok19 = orch2.gcal.retire_calendar("oldcal19@group.calendar.google.com")
patch19 = svc19.calendars.return_value.patch.call_args
del19 = svc19.acl.return_value.delete.call_args_list
check("retire: renamed with [RETIRED] prefix",
      ok19 and patch19.kwargs["body"]["summary"]
      == "[RETIRED] Xin, Tian Portfolio")
check("retire: only the real owner email revoked "
      "(pseudo-owner/PM/SA/default kept)",
      [c.kwargs["ruleId"] for c in del19] == ["r4"])
svc19.calendars.return_value.get.return_value.execute.return_value = {
    "summary": "[RETIRED] Xin, Tian Portfolio"}
svc19.acl.return_value.list.return_value.execute.return_value = {"items": []}
rename19 = svc19.calendars.return_value.patch.call_count
orch2.gcal.retire_calendar("oldcal19@group.calendar.google.com")
check("retire: idempotent (no re-rename when already prefixed)",
      svc19.calendars.return_value.patch.call_count == rename19)

print("\n=== 20. Group cutover: idempotent migration ===")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({
        "_commitments": {"69@10": [{
            "event_id": "oldev", "anchor_date": "2026-08-15",
            "source_type": "status", "origin_month": "2026-07",
            "calendar_id": "oldcal", "covers_rent_month": "2026-07"}]},
        "_calendars": {"10": "oldcal", "11": "oldcal"},  # 2 owners, 1 calendar
        "69@10_2026-07": {"status": "🔴 Unpaid", "past_due": 900.0,
                          "calendar_id": "oldcal"},
    }, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch7 = SyncOrchestrator()

row69 = {"occupancy_id": 69, "tenant": "Burdine, Tyquita",
         "additional_tenants": "", "rent": "900.00", "past_due": "900.00",
         "status": "Current", "property_name": "31 West 112th Place",
         "unit": "2", "property_street": "31 West 112Th Place",
         "property_city": "Chicago", "property_state": "IL",
         "property_zip": "60628", "lease_from": "2025-11-15",
         "lease_to": "2026-11-30"}
g_rows20 = {"g3": [(row69, {"group_id": 3, "group_name": "Test Group"})]}
old_desc = "PM note kept\n" + config.COMMITMENT_DIVIDER + "\nauto stuff"
ins20 = orch7.gcal.service.events.return_value.insert
ins20.return_value.execute.return_value = {"id": "newev1"}
with mock.patch.object(orch7.gcal, "retire_calendar", return_value=True) as ret20, \
     mock.patch.object(orch7.gcal, "get_or_create_group_calendar",
                       return_value="gcal3") as goc20, \
     mock.patch.object(orch7.gcal, "ensure_calendar_summary",
                       return_value=True), \
     mock.patch.object(orch7.gcal, "find_all_events_by_type",
                       return_value=[]), \
     mock.patch.object(orch7.gcal, "get_event",
                       return_value={"description": old_desc}), \
     mock.patch.object(orch7.gcal, "delete_event") as del20:
    meta20 = orch7._run_group_cutover(g_rows20, {}, {}, _date(2026, 7, 19))
check("cutover: one retirement per DISTINCT calendar (2 owners, 1 cal)",
      ret20.call_count == 1 and ret20.call_args == mock.call("oldcal"))
check("cutover: group calendar resolved",
      meta20 == {"g3": ("Test Group", "gcal3")})
sent20 = ins20.call_args.kwargs["body"]
check("cutover: commitment recreated with PM notes preserved",
      ins20.call_count == 1 and sent20["description"].startswith("PM note kept")
      and sent20["extendedProperties"]["private"]["okpm_source_type"] == "status")
check("cutover: old commitment event deleted",
      del20.call_args == mock.call("oldcal", "oldev"))
comms20 = orch7.state.get_commitments("69@g3")
check("cutover: registry re-keyed to @g scope",
      len(comms20) == 1 and comms20[0]["event_id"] == "newev1"
      and comms20[0]["calendar_id"] == "gcal3"
      and comms20[0]["anchor_date"] == "2026-08-15")
check("cutover: legacy commitment key removed",
      "69@10" not in orch7.state.data["_commitments"])
check("cutover: legacy month entries purged, calendars moved to retired",
      "69@10_2026-07" not in orch7.state.data
      and orch7.state.data["_calendars"] == {"g3": "gcal3"}
      and orch7.state.data["_retired_calendars"] == {"10": "oldcal",
                                                     "11": "oldcal"})
check("cutover: marker written", orch7.state.migration_done("group_cutover_v1"))

# Re-run: everything already migrated — zero mutating Google calls.
with mock.patch.object(orch7.gcal, "retire_calendar", return_value=True) as ret21, \
     mock.patch.object(orch7.gcal, "get_or_create_group_calendar") as goc21, \
     mock.patch.object(orch7.gcal, "ensure_calendar_summary",
                       return_value=True), \
     mock.patch.object(orch7.gcal, "find_all_events_by_type",
                       return_value=[]), \
     mock.patch.object(orch7.gcal, "delete_event") as del21:
    orch7._run_group_cutover(g_rows20, {}, {}, _date(2026, 7, 19))
check("cutover re-run: no retire/create/insert/delete calls",
      ret21.call_count == 0 and not goc21.called
      and ins20.call_count == 1 and not del21.called)
os.unlink(tmp)

print("\n=== 21. Fast-path gate: update/submit no-op before cutover ===")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({"_commitments": {}}, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch8 = SyncOrchestrator()
os.unlink(tmp)
with mock.patch.object(orch8.af, "get_rent_roll") as rr21, \
     mock.patch.object(pkg_cache, "load_json") as lj21:
    orch8.run_update()
    orch8.run_submit()
check("update/submit bail out before any pull or cache read",
      not rr21.called and not lj21.called)
orch8.state.mark_migration_done("group_cutover_v1", {"note": "smoke"})
check("marker round-trips with done_at stamp",
      orch8.state.migration_done("group_cutover_v1")
      and "done_at" in orch8.state.data["_migrations"]["group_cutover_v1"])

print("\n=== 22. Day-groups: same-day payments render as one event ===")
pays22 = [
    {"date": "2026-07-03", "amount": 700.0, "is_nsf": False,
     "description": "ACH (#AA-11)", "intended_month": None},
    {"date": "2026-07-03", "amount": 300.0, "is_nsf": False,
     "description": "ACH (#BB-22)", "intended_month": None},
    {"date": "2026-07-03", "amount": 200.0, "is_nsf": True,
     "description": "ACH (#CC-33) NSF", "intended_month": None},
    {"date": "2026-07-10", "amount": 400.0, "is_nsf": False,
     "description": "ACH (#DD-44)", "intended_month": (2026, 6)},
]
groups22 = transforms.group_payments_by_day(pays22)
check("non-NSF same-day rows merged; NSF stays its own group",
      [g["amount"] for g in groups22] == [1000.0, 200.0, 400.0]
      and [len(g["rows"]) for g in groups22] == [2, 1, 1]
      and groups22[1]["is_nsf"] is True)
check("merged group description + intended_month propagation",
      groups22[0]["description"] == "2 same-day payments"
      and groups22[3 - 1]["intended_month"] == (2026, 6))
check("running balances over groups (NSF excluded from add-back)",
      transforms.compute_running_balances(groups22, 100.0)
      == [500.0, 500.0, 100.0])

print("\n=== 23. Settled-collapse state machine ===")
rows23 = [
    {"date": "2026-07-02", "amount": 500.0, "is_nsf": False,
     "description": "ACH (#R1)"},
    {"date": "2026-07-09", "amount": 700.0, "is_nsf": False,
     "description": "ACH (#R2)"},
]
rct = transforms.resolve_collapse_transition
t = rct(None, rows23, 0.0)
check("fully paid -> collapsed, snapshot = all rows, transitioned",
      t["state"] == "collapsed" and t["settled_rows"] == rows23
      and t["transitioned"] and not t["reverted"])
prior23 = {"collapse_state": "collapsed", "collapse_baseline": 2,
           "settled_rows": rows23, "payments": rows23}
t = rct(prior23, rows23, 0.0)
check("steady collapsed run -> no transition",
      t["state"] == "collapsed" and not t["transitioned"])
extra_row = {"date": "2026-07-15", "amount": 100.0, "is_nsf": False,
             "description": "ACH (#R3)"}
t = rct(prior23, rows23 + [extra_row], -100.0)
check("advance payment while collapsed -> stays collapsed, snapshot grows",
      t["state"] == "collapsed" and len(t["settled_rows"]) == 3
      and t["transitioned"])
t = rct(prior23, rows23, 80.0)
check("charge after settlement, no new payment -> frozen",
      t["state"] == "frozen" and t["transitioned"])
prior_frozen = {**prior23, "collapse_state": "frozen"}
t = rct(prior_frozen, rows23, 80.0)
check("steady frozen run -> no transition",
      t["state"] == "frozen" and not t["transitioned"])
t = rct(prior_frozen, rows23 + [extra_row], 30.0)
check("payment toward post-settle charge -> reactivated over fresh rows only",
      t["state"] == "reactivated" and t["fresh"] == [extra_row]
      and t["settled_rows"] == rows23)
t = rct(prior_frozen, [rows23[0]], 700.0)
check("settled row vanished while owing -> collapse REVERTED",
      t["state"] is None and t["reverted"] and t["transitioned"])
t = rct(prior23, [rows23[0], {**rows23[1], "is_nsf": True}], 700.0)
check("settled row reappears NSF-flagged -> collapse REVERTED",
      t["state"] is None and t["reverted"])
prepaid_prior = {"collapse_state": "collapsed", "collapse_baseline": 0,
                 "settled_rows": [], "payments": [],
                 "settled_past_due": -50.0}
check("empty-snapshot prepaid prior heals (no settlement without rows)",
      rct(prepaid_prior, [], 60.0)["state"] is None
      and rct(prepaid_prior, [], 60.0)["healed"]
      and rct(prepaid_prior, [rows23[0]], 60.0)["state"] is None
      and not rct(prepaid_prior, [rows23[0]], 60.0)["reverted"])
legacy23 = {"status": "grey-era", "past_due": 0.0, "payments": rows23}
t = rct(legacy23, rows23, 0.0)
check("legacy grey-era entry collapses on first evaluation",
      t["state"] == "collapsed" and t["transitioned"])
react_prior = {"collapse_state": "reactivated", "collapse_baseline": 2,
               "settled_rows": rows23, "payments": rows23 + [extra_row]}
t = rct(react_prior, rows23 + [extra_row], 40.0)
check("reactivated stays reactivated while balance remains",
      t["state"] == "reactivated" and t["fresh"] == [extra_row]
      and not t["transitioned"])
t = rct(react_prior, rows23 + [extra_row], 0.0)
check("fresh cycle fully paid -> re-collapses with the full snapshot",
      t["state"] == "collapsed" and len(t["settled_rows"]) == 3)
t = rct(react_prior, rows23, 40.0)
check("reactivated fresh rows all vanished -> returns to frozen (canonical)",
      t["state"] == "frozen" and t["transitioned"] and not t["reverted"])
check("legacy fallback: settled_rows absent -> payments[:baseline]",
      rct({"collapse_state": "collapsed", "collapse_baseline": 2,
           "payments": rows23}, rows23, 50.0)["state"] == "frozen")
# Zero-payment collapse gate + bogus-settlement self-heal (the Aug-2026
# rollover bugs: pd reads 0.0 before AppFolio posts the month's charges,
# and a carried credit is NOT a settlement — the incoming rent charge may
# exceed it, leaving a real balance behind a frozen "$0 due" event).
t = rct(None, [], 0.0)
check("zero-payment month at pd==0 never collapses (pre-charge gap)",
      t["state"] is None and not t["transitioned"] and not t["healed"])
t = rct(None, [], -25.0)
check("zero-payment credit month never collapses (credit offsets the charge)",
      t["state"] is None and not t["transitioned"] and not t["healed"])
t = rct(None, rows23, -25.0)
check("credit WITH live rows still collapses (real advance payment)",
      t["state"] == "collapsed" and t["settled_rows"] == rows23)
bogus23 = {"collapse_state": "frozen", "collapse_baseline": 0,
           "settled_rows": [], "payments": [],
           "settled_past_due": 0.0, "settled_on": "2026-08-01"}
t = rct(bogus23, [], 1400.0)
check("bogus empty settlement HEALS: expanded, forced transition, not revert",
      t["state"] is None and t["transitioned"] and t["healed"]
      and not t["reverted"])
t = rct({**bogus23, "collapse_state": "collapsed"}, [], 1400.0)
check("bogus 'collapsed' prior heals identically",
      t["state"] is None and t["transitioned"] and t["healed"])
t = rct({**bogus23, "settled_past_due": -1800.0}, [], 600.0)
check("credit-'settled' prior heals too (Tamika case: charge ate the credit)",
      t["state"] is None and t["healed"] and t["transitioned"])
t = rct({"collapse_state": "frozen", "settled_rows": [], "payments": []},
        [], 700.0)
check("missing settled_past_due heals without TypeError",
      t["state"] is None and t["healed"] and t["transitioned"])
t = rct({"collapse_state": "collapsed", "collapse_baseline": 2,
         "payments": rows23, "settled_past_due": 0.0}, rows23, 50.0)
check("legacy baseline fallback beats the empty-snapshot heal check",
      t["state"] == "frozen" and not t["healed"])
t = rct({"collapse_state": None, "past_due": 1400.0}, [], 1400.0)
check("healed state is steady next run (no write churn)",
      t["state"] is None and not t["transitioned"] and not t["healed"])

print("\n=== 24. Settled-month event + day-group builders ===")
unit24 = {**unit_fx, "past_due": 0.0, "amount_paid": 1400.0}
g24 = transforms.group_payments_by_day([
    {"date": "2026-07-02", "amount": 400.0, "is_nsf": False,
     "description": "ACH (#S1)", "intended_month": None},
    {"date": "2026-07-02", "amount": 300.0, "is_nsf": False,
     "description": "ACH (#S2)", "intended_month": None},
    {"date": "2026-07-16", "amount": 700.0, "is_nsf": False,
     "description": "ACH (#S3)", "intended_month": None},
])
ph24 = [{"event_id": "pe1", "anchor_date": "2026-07-16",
         "source_type": "status", "origin_month": "2026-07",
         "covers_rent_month": "2026-07", "outcome": "kept",
         "recorded": "2026-07-16"}]
sb = orch.gcal._build_settled_month_event(
    unit24, _date(2026, 7, 16), g24, promise_history=ph24,
    reversal_notes=None, settled_on="2026-07-16")
check("settled event: green, anchored on last payment date, total in title",
      sb["colorId"] == "2" and sb["start"]["date"] == "2026-07-16"
      and "$1,400 paid" in sb["summary"]
      and sb["extendedProperties"]["private"]["okpm_event_type"] == "status")
check("settled event: per-row column-0 Amount lines kept (reversal matching)",
      "Amount:       $400.00" in sb["description"]
      and "Amount:       $300.00" in sb["description"]
      and "Amount:       $700.00" in sb["description"])
check("settled event: promise history rendered",
      "Promise history:" in sb["description"]
      and "KEPT (payment received that day)" in sb["description"])
sbp = orch.gcal._build_settled_month_event(
    {**unit24, "past_due": -200.0}, _date(2026, 7, 16), g24,
    settled_on="2026-07-16")
check("credit month -> pink with credit suffix",
      sbp["colorId"] == "4"
      and "credit toward next month" in sbp["description"])
sb0 = orch.gcal._build_settled_month_event(
    {**unit24, "past_due": -50.0, "amount_paid": 0.0}, _date(2026, 7, 1), [],
    settled_on="2026-07-01")
check("pure-prepaid month -> $0 due shape, no payment-history section",
      "$0 due" in sb0["summary"]
      and "Payment history" not in sb0["description"])
st24 = orch.gcal._build_status_event(
    {**unit_fx, "past_due": 700.0, "amount_paid": 700.0},
    status.STATUS_PARTIAL, _date(2026, 7, 2), g24[0], 700.0,
    total_payments=2)
check("status event absorbs the whole first day-group (sum + itemised rows)",
      "$700 paid" in st24["summary"]
      and "(2 same-day payments)" in st24["description"]
      and "Amount:       $400.00" in st24["description"]
      and "Amount:       $300.00" in st24["description"])
pe24 = orch.gcal._build_additional_payment_event(
    {**unit_fx, "past_due": 700.0, "amount_paid": 1400.0}, g24[0],
    2, 2, 700.0, 1400.0)
check("payment event renders a day-group (sum title, per-row blocks, idx=1)",
      "$700" in pe24["summary"]
      and "Amount:       $400.00" in pe24["description"]
      and pe24["extendedProperties"]["private"]["okpm_payment_idx"] == "1")
sp24 = {"count": 2, "total": 700.0, "settled_on": "2026-07-09",
        "rows": g24[0]["rows"]}
st25 = orch.gcal._build_status_event(
    {**unit_fx, "past_due": 100.0, "amount_paid": 1400.0},
    status.STATUS_PARTIAL, _date(2026, 7, 20), g24[1], 100.0,
    total_payments=1, settled_prefix=sp24)
check("reactivated month: 'Previously settled' section + fresh tracking note",
      "Previously settled" in st25["description"]
      and "covers the new balance only" in st25["description"])

print("\n=== 25. Promise absorption by same-day payment ===")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({"_commitments": {"88@g9": [
        {"event_id": "cA", "anchor_date": "2026-07-10",
         "source_type": "status", "origin_month": "2026-07",
         "covers_rent_month": "2026-07", "calendar_id": "calZ"},
        {"event_id": "cB", "anchor_date": "2026-07-20",
         "source_type": "payment", "origin_month": "2026-07",
         "covers_rent_month": "2026-07", "calendar_id": "calZ"},
    ]}}, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch9 = SyncOrchestrator()
os.unlink(tmp)

unit88 = {"occupancy_id": "88", "tenant": "Doe, Jane",
          "additional_tenants": "", "rent": 1000.0, "past_due": 600.0}
# Shared service mock (see note in section 16) — start from a clean counter.
orch9.gcal.service.events.return_value.insert.reset_mock()
live25 = [
    {"id": "cA", "start": {"date": "2026-07-10"}},
    {"id": "cB", "start": {"date": "2026-07-20"}},
]
with mock.patch.object(orch9.gcal, "find_all_events_by_type",
                       return_value=live25), \
     mock.patch.object(orch9.gcal, "delete_event") as del9, \
     mock.patch.object(orch9.gcal, "update_commitment_event",
                       return_value="2026-07-20") as upd9:
    orch9._process_commitments("88@g9", "calZ", unit88, _date(2026, 7, 11),
                               has_known_or_new=True,
                               payment_dates={"2026-07-10"})
check("promise on the payment date absorbed; the other survives",
      del9.call_args_list == [mock.call("calZ", "cA")]
      and [c["event_id"] for c in orch9.state.get_commitments("88@g9")]
      == ["cB"] and upd9.called)
check("absorption recorded as outcome 'kept'",
      any(r["event_id"] == "cA" and r["outcome"] == "kept"
          for r in orch9._pending_promise_history.get(("88@g9", "2026-07"), [])))
check("no ≥1-promise resurrection after absorbing (no inserts)",
      orch9.gcal.service.events.return_value.insert.call_count == 0)
with mock.patch.object(orch9.gcal, "find_all_events_by_type",
                       return_value=[live25[1]]), \
     mock.patch.object(orch9.gcal, "delete_event") as del9b:
    orch9._process_commitments("88@g9", "calZ", unit88, _date(2026, 7, 21),
                               has_known_or_new=True,
                               payment_dates={"2026-07-20"})
check("absorbing the LAST promise leaves ZERO promises (rule not tripped)",
      del9b.call_args_list == [mock.call("calZ", "cB")]
      and orch9.state.get_commitments("88@g9") == []
      and orch9.gcal.service.events.return_value.insert.call_count == 0)
orch9.state.set_commitments("88@g9", [
    {"event_id": "cC", "anchor_date": "2026-07-12", "source_type": "late",
     "origin_month": "2026-07", "covers_rent_month": None,
     "calendar_id": "calZ"}])
live25c = [{"id": "cC", "start": {"date": "2026-07-12"}}]
with mock.patch.object(orch9.gcal, "find_all_events_by_type",
                       return_value=live25c), \
     mock.patch.object(orch9.gcal, "delete_event") as del9c, \
     mock.patch.object(orch9.gcal, "update_commitment_event",
                       return_value="2026-07-12"):
    orch9._process_commitments("88@g9", "calZ", unit88, _date(2026, 7, 12),
                               has_known_or_new=True, payment_dates=None)
check("payment_dates=None (submit: no ledger) -> absorption skipped",
      not del9c.called
      and len(orch9.state.get_commitments("88@g9")) == 1)
with mock.patch.object(orch9.gcal, "find_all_events_by_type",
                       return_value=live25c), \
     mock.patch.object(orch9.gcal, "delete_event") as del9d:
    orch9._process_commitments("88@g9", "calZ", {**unit88, "past_due": 0.0},
                               _date(2026, 7, 13), has_known_or_new=True,
                               payment_dates=set())
check("resolution deletes the promise and records outcome 'resolved'",
      del9d.call_args == mock.call("calZ", "cC")
      and any(r["event_id"] == "cC" and r["outcome"] == "resolved"
              for r in orch9._pending_promise_history.get(("88@g9", "2026-07"),
                                                          [])))
orch9.state.set("88@g9", "2026-07", {"status": "🟡 Partial",
                                     "past_due": 600.0,
                                     "calendar_id": "calZ"})
orch9._flush_pending_promise_history()
hist25 = {(r["event_id"], r["outcome"])
          for r in orch9.state.get("88@g9", "2026-07")["promise_history"]}
check("flush merges pending outcomes into the month entry",
      hist25 >= {("cA", "kept"), ("cB", "kept"), ("cC", "resolved")}
      and orch9._pending_promise_history == {})

# Same-anchor duplicate promises (stale-state race, 2026-08-02 incident):
# two registered promises, same calendar, same source, same live anchor →
# the older registration survives, the copy is deleted from the calendar.
orch9.state.set_commitments("88@g9", [
    {"event_id": "m1", "anchor_date": "2026-07-28", "source_type": "status",
     "origin_month": "2026-07", "covers_rent_month": "2026-07",
     "calendar_id": "calZ"},
    {"event_id": "m2", "anchor_date": "2026-07-28", "source_type": "status",
     "origin_month": "2026-07", "covers_rent_month": "2026-07",
     "calendar_id": "calZ"},
])
live25d = [{"id": "m1", "start": {"date": "2026-07-28"}},
           {"id": "m2", "start": {"date": "2026-07-28"}}]
with mock.patch.object(orch9.gcal, "find_all_events_by_type",
                       return_value=live25d), \
     mock.patch.object(orch9.gcal, "delete_event") as del9e, \
     mock.patch.object(orch9.gcal, "update_commitment_event",
                       return_value="2026-07-28"):
    orch9._process_commitments("88@g9", "calZ", unit88, _date(2026, 7, 14),
                               has_known_or_new=True, payment_dates=set())
check("same-anchor duplicate promise deleted, oldest registration kept",
      del9e.call_args_list == [mock.call("calZ", "m2")]
      and [c["event_id"] for c in orch9.state.get_commitments("88@g9")]
      == ["m1"])
# A real split plan (same source, DIFFERENT anchors) is never deduped.
orch9.state.set_commitments("88@g9", [
    {"event_id": "s1", "anchor_date": "2026-07-20", "source_type": "status",
     "origin_month": "2026-07", "covers_rent_month": "2026-07",
     "calendar_id": "calZ"},
    {"event_id": "s2", "anchor_date": "2026-07-27", "source_type": "status",
     "origin_month": "2026-07", "covers_rent_month": "2026-07",
     "calendar_id": "calZ"},
])
live25e = [{"id": "s1", "start": {"date": "2026-07-20"}},
           {"id": "s2", "start": {"date": "2026-07-27"}}]
with mock.patch.object(orch9.gcal, "find_all_events_by_type",
                       return_value=live25e), \
     mock.patch.object(orch9.gcal, "delete_event") as del9f, \
     mock.patch.object(orch9.gcal, "update_commitment_event",
                       return_value="2026-07-20"):
    orch9._process_commitments("88@g9", "calZ", unit88, _date(2026, 7, 14),
                               has_known_or_new=True, payment_dates=set())
check("split plan on different dates untouched by the dedupe",
      not del9f.called
      and len(orch9.state.get_commitments("88@g9")) == 2)
orch9.state.set_commitments("88@g9", [])

# Mirror hardening: the sibling registry is stale/empty, but the sibling
# CALENDAR already carries the same-anchor promise → adopt, never insert.
orch9._groups_by_oid["88"] = [("g9", "calZ"), ("gX", "calX")]
orch9.state.set_commitments("88@gX", [])
sib_live = [{"id": "mx1", "start": {"date": "2026-07-28"},
             "extendedProperties": {"private":
                                    {"okpm_source_type": "status"}}}]
_commit25 = {"event_id": "cm0", "anchor_date": "2026-07-28",
             "source_type": "status", "origin_month": "2026-07",
             "calendar_id": "calZ", "covers_rent_month": "2026-07"}
ins_before = orch9.gcal.service.events.return_value.insert.call_count
with mock.patch.object(orch9.gcal, "find_all_events_by_type",
                       return_value=sib_live):
    orch9._mirror_commitment_to_siblings("88", _commit25, unit88,
                                         _date(2026, 7, 14))
check("mirror adopts an existing same-anchor sibling event (stale registry)",
      orch9.gcal.service.events.return_value.insert.call_count == ins_before
      and [c["event_id"] for c in orch9.state.get_commitments("88@gX")]
      == ["mx1"])
orch9.state.set_commitments("88@gX", [])
orch9._groups_by_oid.pop("88", None)

print("\n=== 26. Surplus payment-event cleanup (collapse + leak fix) ===")
orch9.state.set("90@g9", "2026-07", {"calendar_id": "calY",
                                     "nsf_event_ids": ["n1"]})
with mock.patch.object(orch9.gcal, "delete_event") as d10, \
     mock.patch.object(orch9.gcal, "find_month_payment_events",
                       return_value=[{"id": "stray"}]):
    orch9._cleanup_surplus_payment_events(
        "90@g9", "calY", "90", "2026-07",
        keep_ids={"k1"}, prior_payment_ids=["k1", "old1"],
        collapsed=False, live_scan=True)
check("expanded: keeps current + NSF ids, deletes stale prior + live strays",
      sorted(c.args[1] for c in d10.call_args_list) == ["old1", "stray"])
with mock.patch.object(orch9.gcal, "delete_event") as d11, \
     mock.patch.object(orch9.gcal, "find_month_payment_events",
                       return_value=[{"id": "p1"}, {"id": "n1"}]):
    orch9._cleanup_surplus_payment_events(
        "90@g9", "calY", "90", "2026-07",
        keep_ids=set(), prior_payment_ids=["p1"],
        collapsed=True, live_scan=True)
check("collapsed: ALL payment events deleted, nsf_event_ids cleared",
      sorted(c.args[1] for c in d11.call_args_list) == ["n1", "p1"]
      and orch9.state.get("90@g9", "2026-07")["nsf_event_ids"] == [])
with mock.patch.object(orch9.gcal, "delete_event") as d12, \
     mock.patch.object(orch9.gcal, "find_month_payment_events") as fm12:
    orch9._cleanup_surplus_payment_events(
        "90@g9", "calY", "90", "2026-07",
        keep_ids={"k1"}, prior_payment_ids=["k1"],
        collapsed=False, live_scan=False)
check("steady hourly run: no live scan, nothing deleted",
      not fm12.called and not d12.called)

print("\n=== 27. Reversal against a settled prior month -> un-collapse ===")
june_rows = [
    {"date": "2026-06-05", "amount": 500.0, "is_nsf": False,
     "description": "ACH (#JJ-55)"},
    {"date": "2026-06-20", "amount": 1000.0, "is_nsf": False,
     "description": "ACH (#KK-66)"},
]
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({
        "_commitments": {},
        "85@10_2026-06": {"fmt": 2, "status": "✅ Paid", "past_due": 0.0,
                          "calendar_id": "calA", "status_event_id": "set6",
                          "status_event_date": "2026-06-20",
                          "late_event_id": None, "payment_event_ids": [],
                          "payment_event_dates": [], "payments": june_rows,
                          "collapse_state": "collapsed",
                          "collapse_baseline": 2, "settled_rows": june_rows,
                          "settled_past_due": 0.0,
                          "settled_on": "2026-06-20", "promise_history": []},
        "85@10_2026-07": {"fmt": 2, "status": "🔴 Unpaid", "past_due": 1000.0,
                          "calendar_id": "calA", "status_event_id": "st7c",
                          "status_event_date": "2026-07-01",
                          "late_event_id": None, "payment_event_ids": []},
    }, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch11 = SyncOrchestrator()
os.unlink(tmp)

rmap27 = {"Sanquia Darden": [
    {"date": "2026-07-02", "amount": 1000.0, "ref": "KK-66",
     "description": "NSF reversal receipt for Reference #KK-66"}]}
with mock.patch.object(orch11.gcal, "_update_or_create",
                       side_effect=["set6", "newP"]) as uoc27, \
     mock.patch.object(orch11.gcal, "_find_payment_event",
                       return_value=None), \
     mock.patch.object(orch11.gcal, "get_event", return_value=None):
    orch11._apply_nsf_reversals("85@10", "calA", unit85, _date(2026, 7, 4),
                                "2026-07", rmap27,
                                surplus_payment_ids=[], prior_payments=[])
stat27 = uoc27.call_args_list[0].args[2]
pay27  = uoc27.call_args_list[1].args[2]
check("un-collapse: status event rebuilt yellow on first pay date, in place",
      uoc27.call_args_list[0].args[1] == "set6"
      and stat27["start"]["date"] == "2026-06-05"
      and stat27["colorId"] == "5"
      and "month no longer settled" in stat27["description"]
      and "reconstructed from sync records" in stat27["description"])
check("un-collapse: bounced payment rebuilt as its own RED event",
      pay27["colorId"] == "11" and " NSF" in pay27["summary"]
      and pay27["start"]["date"] == "2026-06-20")
june27 = orch11.state.get("85@10", "2026-06")
check("un-collapse: state expanded, bounced row flagged, marker v2",
      june27["collapse_state"] is None
      and june27["past_due"] == 1000.0
      and june27["payments"][1]["is_nsf"] is True
      and [r["key"] for r in june27["nsf_reversals_applied"]] == ["KK-66"]
      and june27["payment_event_ids"] == ["newP"]
      and june27["settled_rows"] == [])
with mock.patch.object(orch11.gcal, "_update_or_create") as uoc27b, \
     mock.patch.object(orch11.gcal, "get_event", return_value=None):
    orch11._apply_nsf_reversals("85@10", "calA", unit85, _date(2026, 7, 4),
                                "2026-07", rmap27,
                                surplus_payment_ids=[], prior_payments=[])
check("second pass is a no-op (marker honored)", not uoc27b.called)

print("\n=== 28. Promise-history projection & merge ===")
comms28 = [
    {"event_id": "x1", "anchor_date": "2026-07-10", "source_type": "status",
     "origin_month": "2026-07", "covers_rent_month": "2026-07",
     "calendar_id": "calQ"},
    {"event_id": "x2", "anchor_date": "2026-08-01",
     "source_type": "kickstart", "origin_month": "2026-08",
     "covers_rent_month": "2026-08", "calendar_id": "calQ"},
    {"event_id": "x3", "anchor_date": "2026-07-18", "source_type": "late",
     "origin_month": "2026-07", "covers_rent_month": None,
     "calendar_id": "other"},
]
proj = orch._project_promise_outcomes(comms28, "calQ", {"2026-07-10"}, 0.0)
check("projection: kept beats resolved; kickstart + other-calendar skipped",
      [(r["event_id"], r["outcome"]) for r in proj] == [("x1", "kept")])
proj2 = orch._project_promise_outcomes(comms28, "calQ", set(), 0.0)
check("projection: resolved when settled",
      [(r["event_id"], r["outcome"]) for r in proj2] == [("x1", "resolved")])
merged28 = orch._merge_promise_history(proj, proj + proj2)
check("merge dedupes by (event_id, outcome)",
      [(r["event_id"], r["outcome"]) for r in merged28]
      == [("x1", "kept"), ("x1", "resolved")])
# Live anchors override registry anchors — the projection must agree with
# the absorption pass when the PM re-dragged a promise since the last run.
proj3 = orch._project_promise_outcomes(
    comms28, "calQ", {"2026-07-10"}, 900.0,
    live_anchor_by_id={"x1": "2026-07-11"})
proj4 = orch._project_promise_outcomes(
    comms28, "calQ", {"2026-07-11"}, 900.0,
    live_anchor_by_id={"x1": "2026-07-11"})
check("projection honours LIVE anchors (re-drag off/on a payment date)",
      proj3 == [] and [(r["event_id"], r["outcome"], r["anchor_date"])
                       for r in proj4] == [("x1", "kept", "2026-07-11")])

print("\n=== 29. Reversals on settled months + surviving-row ghost guard ===")
mm_row = {"date": "2026-07-15", "amount": 1500.0, "is_nsf": False,
          "description": "ACH (#MM-77)"}
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({
        "_commitments": {},
        "77@g2_2026-07": {"fmt": 2, "status": "✅ Paid", "past_due": 0.0,
                          "calendar_id": "calB", "status_event_id": "sev7",
                          "status_event_date": "2026-07-15",
                          "late_event_id": None, "payment_event_ids": [],
                          "payment_event_dates": [], "payments": [mm_row],
                          "collapse_state": "collapsed",
                          "collapse_baseline": 1, "settled_rows": [mm_row],
                          "settled_past_due": 0.0,
                          "settled_on": "2026-07-15",
                          "promise_history": [],
                          "nsf_reversals_applied": [], "nsf_event_ids": []},
        "78@g2_2026-07": {"fmt": 2, "status": "🟡 Partial", "past_due": 850.0,
                          "calendar_id": "calB", "status_event_id": "st78",
                          "status_event_date": "2026-07-03",
                          "late_event_id": None, "payment_event_ids": [],
                          "payment_event_dates": [], "payments": []},
    }, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch12 = SyncOrchestrator()
os.unlink(tmp)
orch12.gcal.service.events.return_value.insert.reset_mock()

# (a) Settle + bounce in the SAME poll: a matched surplus marker on a
# settled month is deleted (never flipped — the cleanup would kill a
# flipped marker anyway) and the bounce is noted on the settled event.
bodies29 = {
    "sev7": {"id": "sev7", "colorId": "2",
             "summary": "✅ · Jane Doe · Unit 1 · 5 Main · $1,500 paid",
             "description": "Received in July: $1,500.00\nStatus:       ✅ Paid"},
    "sur1": {"id": "sur1", "colorId": "2",
             "summary": "✅ · Jane Doe · Unit 1 · 5 Main · $200",
             "description": ("Method:       ACH (#LL-88)\n"
                             "Amount:       $200.00\nStatus:       ✅ Paid")},
}
unit77b = {"occupancy_id": "77", "tenant": "Doe, Jane",
           "additional_tenants": "", "rent": 1500.0, "past_due": 0.0,
           "payments": [mm_row]}
rmap29 = {"Jane Doe": [
    {"date": "2026-07-16", "amount": 200.0, "ref": "LL-88",
     "description": "NSF reversal receipt for Reference #LL-88"}]}
with mock.patch.object(orch12.gcal, "get_event",
                       side_effect=lambda cal, eid: bodies29.get(eid)), \
     mock.patch.object(orch12.gcal, "delete_event") as del29, \
     mock.patch.object(orch12.gcal, "flip_event_to_nsf") as flip29:
    orch12._apply_nsf_reversals("77@g2", "calB", unit77b, _date(2026, 7, 16),
                                "2026-07", rmap29,
                                surplus_payment_ids=["sur1"],
                                prior_payments=[
                                    {"date": "2026-07-14", "amount": 200.0,
                                     "is_nsf": False,
                                     "description": "ACH (#LL-88)"}])
july29 = orch12.state.get("77@g2", "2026-07")
check("settled month: bounce noted on settled event, marker deleted, no flip",
      not flip29.called
      and del29.call_args == mock.call("calB", "sur1")
      and "REVERSED (NSF)" in bodies29["sev7"]["description"]
      and [r["key"] for r in july29["nsf_reversals_applied"]] == ["LL-88"]
      and july29.get("nsf_event_ids") == []
      and orch12.gcal.service.events.return_value.insert.call_count == 0)

# (b) Surviving-row guard: the bounced positive row REMAINED in the pull
# (keyword-flagged NSF) — its natural red rendering covers the bounce, so
# no ghost is reconstructed; the status event still gets the note.
unit78 = {"occupancy_id": "78", "tenant": "Roe, Rick",
          "additional_tenants": "", "rent": 900.0, "past_due": 850.0,
          "payments": [{"date": "2026-07-03", "amount": 650.0,
                        "is_nsf": True,
                        "description": "ACH (#NN-99) NSF returned"}]}
rmap29b = {"Rick Roe": [
    {"date": "2026-07-05", "amount": 650.0, "ref": "NN-99",
     "description": "NSF reversal receipt for Reference #NN-99"}]}
with mock.patch.object(orch12.gcal, "get_event",
                       side_effect=lambda cal, eid: bodies29.get(eid, {
                           "id": eid, "description": "", "summary": ""})), \
     mock.patch.object(orch12.gcal, "delete_event"):
    orch12._apply_nsf_reversals("78@g2", "calB", unit78, _date(2026, 7, 6),
                                "2026-07", rmap29b,
                                surplus_payment_ids=[],
                                prior_payments=[
                                    {"date": "2026-07-03", "amount": 650.0,
                                     "is_nsf": False,
                                     "description": "ACH (#NN-99)"}])
july29b = orch12.state.get("78@g2", "2026-07")
check("surviving NSF-flagged row: marker written but NO duplicate ghost",
      orch12.gcal.service.events.return_value.insert.call_count == 0
      and "NN-99" in [r["key"] for r in july29b["nsf_reversals_applied"]]
      and not july29b.get("nsf_event_ids"))

print("\n=== 30. Copy classifier (untagged UI copies) ===")
from pm_calendar_sync import config as _cfg
from pm_calendar_sync import orchestrator as _orch_mod

b30_status = orch.gcal._build_status_event(
    unit_fx, status.STATUS_UNPAID, _date(2026, 8, 1), None, None,
    total_payments=0)
c30 = transforms.classify_sync_copy(
    b30_status["summary"], b30_status["description"])
check("status (no payments) copy -> status/status + tenant",
      c30 and c30["kind"] == "status" and c30["source_type"] == "status"
      and c30["tenant"] == "Tyquita Burdine")

pay30 = {"date": "2026-08-02", "amount": 700.0, "is_nsf": False,
         "description": "ACH (#A1)", "intended_month": None}
unit30p = {**unit_fx, "amount_paid": 700.0, "payments": [pay30]}
b30_statp = orch.gcal._build_status_event(
    unit30p, status.STATUS_PARTIAL, _date(2026, 8, 2), pay30, 700.0,
    total_payments=1)
check("status (with payment) copy -> status",
      (transforms.classify_sync_copy(
          b30_statp["summary"], b30_statp["description"]) or {}).get("kind")
      == "status")

grp30 = transforms.group_payments_by_day([pay30])
b30_set = orch.gcal._build_settled_month_event(
    {**unit_fx, "past_due": 0.0, "amount_paid": 700.0},
    _date(2026, 8, 2), grp30, settled_on="2026-08-02")
c30s = transforms.classify_sync_copy(b30_set["summary"], b30_set["description"])
check("settled copy -> settled_status (source status)",
      c30s and c30s["kind"] == "settled_status"
      and c30s["source_type"] == "status")

b30_ph = orch.gcal._build_future_placeholder(
    {**unit_fx, "past_due": 0.0}, status.STATUS_UNPAID, _date(2026, 10, 1))
c30p = transforms.classify_sync_copy(b30_ph["summary"], b30_ph["description"])
check("placeholder copy -> kickstart with Late-After month",
      c30p and c30p["kind"] == "placeholder"
      and c30p["source_type"] == "kickstart"
      and c30p["late_after_month"] == "2026-10")

b30_pay = orch.gcal._build_additional_payment_event(
    unit30p, pay30, 2, 2, 700.0, 700.0)
c30pay = transforms.classify_sync_copy(
    b30_pay["summary"], b30_pay["description"])
check("payment copy -> payment/payment",
      c30pay and c30pay["kind"] == "payment"
      and c30pay["source_type"] == "payment")

b30_ghost = orch.gcal._build_nsf_ghost_event(
    unit_fx, {"date": "2026-08-03", "amount": 650.0, "is_nsf": True,
              "description": "ACH (#R2)"}, "reversal recorded", "2026-08")
c30g = transforms.classify_sync_copy(
    b30_ghost["summary"], b30_ghost["description"])
check("NSF ghost copy -> payment source",
      c30g and c30g["kind"] == "nsf_ghost"
      and c30g["source_type"] == "payment")

check("plain personal event -> None",
      transforms.classify_sync_copy(
          "Dentist", "Monthly Rent: ask about invoice") is None)
check("emoji summary + free-text body -> None",
      transforms.classify_sync_copy(
          "🔴 · Someone · Somewhere", "call the plumber") is None)
check("moved-out marker copy -> None (📦 not adoptable)",
      transforms.classify_sync_copy(
          "📦 · Eric · Unit 1 · 3858 W Jackson · moved out",
          "Moved out:    Jul 31, 2026") is None)
auto_only = divider_desc.split(transforms.COMMITMENT_DIVIDER, 1)[1]
check("divider-stripped commitment body -> None (Tenant: != Tenant(s):)",
      transforms.classify_sync_copy(b30_status["summary"], auto_only) is None)
check("mangled Late After -> placeholder month None",
      transforms.classify_sync_copy(
          b30_ph["summary"],
          b30_ph["description"].replace("Late After:", "Late Never:")
      )["late_after_month"] is None)

ident30 = transforms.parse_sync_event_identity(
    "🔴 · Eric Johnson · Unit 1 · 3858 W Jackson · $12,120 due",
    "3858 W Jackson, Chicago, IL, 60624")
check("identity parser: unit form",
      ident30 == {"tenant": "Eric Johnson", "unit_label": "Unit 1",
                  "property_name": "3858 W Jackson",
                  "address": "3858 W Jackson, Chicago, IL, 60624"})
ident30b = transforms.parse_sync_event_identity(
    "✅ · Jane Roe · 8142 S Yates · $900 paid")
check("identity parser: no-unit form",
      ident30b["tenant"] == "Jane Roe" and ident30b["unit_label"] == ""
      and ident30b["property_name"] == "8142 S Yates")

print("\n=== 31. Copy = drag: adoption matrix (status/payment/placeholder) ===")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({"_commitments": {},
               "99@g9_2026-10": {"status": "🔴 Unpaid", "past_due": 0.0,
                                 "calendar_id": "calA",
                                 "rent_event_id": "ph10",
                                 "late_event_id": None}}, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch31 = SyncOrchestrator()
os.unlink(tmp)
_t31 = _date(2026, 8, 15)
upd31 = orch31.gcal.service.events.return_value.update
rows31 = [(row_fx, {"group_id": 9, "group_name": "Bowei Yan"})]

# status copy pasted on a future date → promise (source status, covers now)
copy_status = {"id": "cpS", "start": {"date": "2026-08-20"},
               "summary": b30_status["summary"],
               "description": b30_status["description"]}
with mock.patch.object(orch31, "_mirror_commitment_to_siblings") as mir31:
    n31 = orch31._adopt_untagged_copies(
        rows31, "g9", "calA", {}, {}, _t31, candidates=[copy_status])
body31 = upd31.call_args.kwargs["body"]
c31 = [c for c in orch31.state.get_commitments("99@g9")
       if c["event_id"] == "cpS"]
check("status copy adopted as promise (tags + registry + mirror)",
      n31 == 1 and mir31.called
      and body31["extendedProperties"]["private"]["okpm_event_type"]
      == "commitment"
      and body31["extendedProperties"]["private"]["okpm_source_type"]
      == "status"
      and len(c31) == 1 and c31[0]["anchor_date"] == "2026-08-20"
      and c31[0]["covers_rent_month"] == "2026-08"
      and c31[0]["origin_month"] == "2026-08"
      and "cpS" in orch31._fresh_commitments)

# timed paste (dateTime) → date part anchors; rebuild is all-day
upd31.reset_mock()
copy_timed = {"id": "cpT", "start": {"dateTime": "2026-08-22T14:00:00-05:00"},
              "summary": b30_pay["summary"],
              "description": b30_pay["description"]}
with mock.patch.object(orch31, "_mirror_commitment_to_siblings"):
    orch31._adopt_untagged_copies(
        rows31, "g9", "calA", {}, {}, _t31, candidates=[copy_timed])
bodyT = upd31.call_args.kwargs["body"]
cT = [c for c in orch31.state.get_commitments("99@g9")
      if c["event_id"] == "cpT"]
check("timed payment copy -> all-day promise on the date part",
      cT and cT[0]["anchor_date"] == "2026-08-22"
      and cT[0]["source_type"] == "payment"
      and bodyT["start"] == {"date": "2026-08-22"})

# placeholder copy → kickstart for the Late-After month; original consumed
upd31.reset_mock()
copy_ph = {"id": "cpP", "start": {"date": "2026-09-20"},
           "summary": b30_ph["summary"],
           "description": b30_ph["description"]}
with mock.patch.object(orch31, "_mirror_commitment_to_siblings"), \
     mock.patch.object(orch31.gcal, "delete_event") as del31:
    orch31._adopt_untagged_copies(
        rows31, "g9", "calA", {}, {}, _t31, candidates=[copy_ph])
cP = [c for c in orch31.state.get_commitments("99@g9")
      if c["event_id"] == "cpP"]
oct_entry = orch31.state.get("99@g9", "2026-10")
check("placeholder copy -> kickstart (origin = covers = Late-After month)",
      cP and cP[0]["source_type"] == "kickstart"
      and cP[0]["origin_month"] == "2026-10"
      and cP[0]["covers_rent_month"] == "2026-10")
check("kickstart copy consumes the original placeholder",
      del31.call_args == mock.call("calA", "ph10")
      and oct_entry.get("rent_event_id") is None
      and oct_entry.get("is_commitment") is True)

# mangled Late After → skipped, no writes
upd31.reset_mock()
bad_ph = {"id": "cpBad", "start": {"date": "2026-09-21"},
          "summary": b30_ph["summary"],
          "description": b30_ph["description"].replace("Late After:",
                                                       "Late Never:")}
with mock.patch.object(orch31, "_mirror_commitment_to_siblings"):
    nbad = orch31._adopt_untagged_copies(
        rows31, "g9", "calA", {}, {}, _t31, candidates=[bad_ph])
check("mangled placeholder copy skipped, no writes",
      nbad == 0 and not upd31.called)

# ambiguous tenant (status kind) → skipped
twin31 = {**row_fx, "occupancy_id": 98}
with mock.patch.object(orch31, "_mirror_commitment_to_siblings"):
    namb = orch31._adopt_untagged_copies(
        [(row_fx, {}), (twin31, {})], "g9", "calA", {}, {}, _t31,
        candidates=[{**copy_status, "id": "cpAmb"}])
check("ambiguous status copy skipped, no writes",
      namb == 0 and not upd31.called)

# settled copy on a paid-up unit insta-resolves in the same run's pass
row_paid = {**row_fx, "past_due": "0.00"}
copy_set = {"id": "cpDone", "start": {"date": "2026-08-18"},
            "summary": b30_set["summary"],
            "description": b30_set["description"]}
with mock.patch.object(orch31, "_mirror_commitment_to_siblings"):
    orch31._adopt_untagged_copies(
        [(row_paid, {})], "g9", "calA", {}, {}, _t31,
        candidates=[copy_set])
adopted_body = orch31._fresh_commitments["cpDone"]
with mock.patch.object(orch31.gcal, "delete_event") as delset, \
     mock.patch.object(orch31.gcal, "update_commitment_event",
                       return_value="2026-08-18"):
    orch31._process_commitments(
        "99@g9", "calA", {"occupancy_id": "99", "rent": 1400.0,
                          "past_due": 0.0},
        _t31, has_known_or_new=True, events=[adopted_body])
check("settled copy adopts then insta-resolves (balance <= 0)",
      mock.call("calA", "cpDone") in delset.call_args_list
      and not any(c["event_id"] == "cpDone"
                  for c in orch31.state.get_commitments("99@g9")))

print("\n=== 32. Relaxed drag gates (split plans are first-class) ===")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({"_commitments": {"88@g9": [
        {"event_id": "prior-promise", "anchor_date": "2026-08-25",
         "source_type": "status", "origin_month": "2026-08",
         "calendar_id": "calZ", "covers_rent_month": "2026-08"}]},
        "88@g9_2026-08": {"status": "🔴 Unpaid", "past_due": 900.0,
                          "calendar_id": "calZ", "status_event_id": "st88",
                          "status_event_date": "2026-08-01",
                          "late_event_id": None,
                          "payment_event_ids": ["pm88"],
                          "payment_event_dates": ["2026-08-05"]}}, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch32 = SyncOrchestrator()
os.unlink(tmp)
unit88 = {**unit_fx, "occupancy_id": "88", "rent": 900.0, "past_due": 900.0}
_t32 = _date(2026, 8, 15)
conv_body32 = {"id": "st88", "start": {"date": "2026-08-20"},
               "extendedProperties": {"private": {
                   "okpm_occupancy_id": "88",
                   "okpm_event_type": "commitment",
                   "okpm_source_type": "status"}}}
listed32 = [
    {"id": "st88", "start": {"date": "2026-08-20"},
     "extendedProperties": {"private": {
         "okpm_occupancy_id": "88", "okpm_event_type": "status"}}},
    {"id": "pm88", "start": {"date": "2026-08-28"},
     "extendedProperties": {"private": {
         "okpm_occupancy_id": "88", "okpm_event_type": "payment"}}},
]
ins32 = orch32.gcal.service.events.return_value.insert
ins32.return_value.execute.return_value = {"id": "spawned-pm"}
with mock.patch.object(orch32.gcal, "convert_to_commitment",
                       return_value=conv_body32) as conv32, \
     mock.patch.object(orch32.gcal, "revert_event_to_date") as rev32, \
     mock.patch.object(orch32, "_mirror_commitment_to_siblings"):
    orch32._detect_and_convert_drags(
        "88@g9", "calZ", unit88, _t32, "2026-08", listed32)
anchors32 = {(c["source_type"], c["anchor_date"])
             for c in orch32.state.get_commitments("88@g9")}
check("status drag converts even though a promise already covers the month",
      conv32.called
      and ("status", "2026-08-20") in anchors32
      and ("status", "2026-08-25") in anchors32)
check("payment drag spawns a promise while covered, then snaps back",
      ("payment", "2026-08-28") in anchors32
      and mock.call("calZ", "pm88", "2026-08-05") in rev32.call_args_list)

print("\n=== 33. Q9: suppression is creation-only; cleanup narrowed ===")
rss = transforms.resolve_status_suppression
prom = [{"source_type": "status", "origin_month": "2026-08",
         "covers_rent_month": "2026-08", "event_id": "p1"}]
kick = [{"source_type": "kickstart", "origin_month": "2026-08",
         "covers_rent_month": "2026-08", "event_id": "k1"}]
check("promise-covered + tracked original -> NOT suppressed (Q9)",
      rss(prom, "2026-08", False, "stX")["suppress"] is False
      and rss(prom, "2026-08", False, "stX")["covered"] is True)
check("promise-covered + no tracked original -> suppressed",
      rss(prom, "2026-08", False, None)["suppress"] is True)
check("kickstart-covered -> suppressed (legacy semantics)",
      rss(kick, "2026-08", False, "stX")["suppress"] is True
      and rss(kick, "2026-08", False, "stX")["kickstart_covers"] is True)
check("payments present -> never suppressed",
      rss(prom, "2026-08", True, None)["suppress"] is False)
check("uncovered month -> nothing",
      rss([], "2026-08", False, None) ==
      {"covered": False, "kickstart_covers": False, "suppress": False})

with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"):
    orch33 = SyncOrchestrator()
month_events = {
    "rent":   [{"id": "rDebris", "start": {"date": "2026-08-01"}}],
    "status": [{"id": "stCanon", "start": {"date": "2026-08-01"}},
               {"id": "stMoved", "start": {"date": "2026-08-21"}}],
}
prior33 = {"status_event_date": "2026-08-01"}
with mock.patch.object(orch33.gcal, "find_month_events",
                       side_effect=lambda cal, oid, m, t: month_events[t]), \
     mock.patch.object(orch33.gcal, "delete_event") as del33:
    orch33._cleanup_covered_month_leftovers(
        "88", "calZ", "2026-08", prior33, set(),
        kickstart_covers=False, due_date=_date(2026, 8, 1))
check("promise cover: only rent debris deleted (Q9 original untouched)",
      del33.call_args_list == [mock.call("calZ", "rDebris")])
with mock.patch.object(orch33.gcal, "find_month_events",
                       side_effect=lambda cal, oid, m, t: month_events[t]), \
     mock.patch.object(orch33.gcal, "delete_event") as del33b:
    orch33._cleanup_covered_month_leftovers(
        "88", "calZ", "2026-08", prior33, set(),
        kickstart_covers=True, due_date=_date(2026, 8, 1))
check("kickstart cover: canonical status deleted, MOVED status left "
      "for drag detection",
      mock.call("calZ", "stCanon") in del33b.call_args_list
      and mock.call("calZ", "stMoved") not in del33b.call_args_list
      and mock.call("calZ", "rDebris") in del33b.call_args_list)

print("\n=== 34. Drag + copy interleaves (any order, dedup on same date) ===")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({"_commitments": {},
               "99@g9_2026-08": {"status": "🔴 Unpaid", "past_due": 700.0,
                                 "calendar_id": "calA",
                                 "status_event_id": "st99",
                                 "status_event_date": "2026-08-01",
                                 "late_event_id": None,
                                 "payment_event_ids": [],
                                 "payment_event_dates": []}}, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch34 = SyncOrchestrator()
os.unlink(tmp)
unit99 = {"occupancy_id": "99", "rent": 1400.0, "past_due": 700.0}
_t34 = _date(2026, 8, 15)
conv_body34 = {"id": "st99", "start": {"date": "2026-08-20"},
               "extendedProperties": {"private": {
                   "okpm_occupancy_id": "99",
                   "okpm_event_type": "commitment",
                   "okpm_source_type": "status"}}}

# (a) copy adopted, then the drag converts in the SAME submit run
with mock.patch.object(orch34, "_mirror_commitment_to_siblings"):
    orch34._adopt_untagged_copies(
        rows31, "g9", "calA", {}, {}, _t34,
        candidates=[{"id": "cpB", "start": {"date": "2026-08-25"},
                     "summary": b30_status["summary"],
                     "description": b30_status["description"]}])
listed34 = [{"id": "st99", "start": {"date": "2026-08-20"},
             "extendedProperties": {"private": {
                 "okpm_occupancy_id": "99", "okpm_event_type": "status"}}}]
with mock.patch.object(orch34.gcal, "convert_to_commitment",
                       return_value=conv_body34) as conv34, \
     mock.patch.object(orch34, "_mirror_commitment_to_siblings"):
    orch34._detect_and_convert_drags(
        "99@g9", "calA", unit99, _t34, "2026-08", listed34)
a34 = {c["anchor_date"] for c in orch34.state.get_commitments("99@g9")}
check("copy then drag, same run: BOTH promises survive",
      conv34.called and a34 == {"2026-08-25", "2026-08-20"})

# (b) drag onto the SAME date as the copy → dedup keeps the older (copy)
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({"_commitments": {"99@g9": [
        {"event_id": "cpB", "anchor_date": "2026-08-25",
         "source_type": "status", "origin_month": "2026-08",
         "calendar_id": "calA", "covers_rent_month": "2026-08"},
        {"event_id": "st99", "anchor_date": "2026-08-25",
         "source_type": "status", "origin_month": "2026-08",
         "calendar_id": "calA", "covers_rent_month": "2026-08"}]}}, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch34b = SyncOrchestrator()
os.unlink(tmp)
live34b = [{"id": "cpB", "start": {"date": "2026-08-25"},
            "extendedProperties": {"private": {
                "okpm_occupancy_id": "99", "okpm_event_type": "commitment",
                "okpm_source_type": "status"}}},
           {"id": "st99", "start": {"date": "2026-08-25"},
            "extendedProperties": {"private": {
                "okpm_occupancy_id": "99", "okpm_event_type": "commitment",
                "okpm_source_type": "status"}}}]
with mock.patch.object(orch34b.gcal, "delete_event") as del34, \
     mock.patch.object(orch34b.gcal, "update_commitment_event",
                       return_value="2026-08-25"):
    orch34b._process_commitments(
        "99@g9", "calA", unit99, _t34, has_known_or_new=True,
        events=live34b)
c34b = orch34b.state.get_commitments("99@g9")
check("drag onto the copy's date: same-anchor dedup keeps the older copy",
      [c["event_id"] for c in c34b] == ["cpB"]
      and mock.call("calA", "st99") in del34.call_args_list)

print("\n=== 35. Notice rows sync; horizon cap; beyond-horizon prune ===")
roll35 = [
    {"occupancy_id": 65, "status": "Notice-Unrented", "tenant": "A, B",
     "move_out": "2026-08-31"},
    {"occupancy_id": 1, "status": "Current", "tenant": "C, D"},
    {"occupancy_id": None, "status": "Vacant-Unrented",
     "last_move_out": "2026-07-31"},
]
check("active_rows keeps every row with an occupancy_id",
      [r.get("occupancy_id") for r in transforms.active_rows(roll35)]
      == [65, 1])
rlh = transforms.resolve_lease_horizon
check("horizon: move_out caps a missing lease_to (fallback first)",
      rlh("", "2026-08-31", _date(2026, 8, 1), 12) == _date(2026, 8, 31))
check("horizon: lease_to alone unchanged",
      rlh("2026-11-30", "", _date(2026, 8, 1), 12) == _date(2026, 11, 30))
check("horizon: past move_out pulls the horizon before next month",
      rlh("2027-08-01", "2026-07-31", _date(2026, 8, 1), 12)
      == _date(2026, 7, 31))
check("horizon: mangled move_out ignored",
      rlh("2026-11-30", "soon", _date(2026, 8, 1), 12) == _date(2026, 11, 30))

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({"_commitments": {},
               "65@g9_2026-09": {"past_due": 0.0, "calendar_id": "calA",
                                 "rent_event_id": "r9",
                                 "late_event_id": None},
               "65@g9_2026-10": {"past_due": 0.0, "calendar_id": "calA",
                                 "rent_event_id": "r10",
                                 "is_commitment": True,
                                 "late_event_id": None},
               "65@g9_2026-11": {"past_due": 0.0, "calendar_id": "calOTHER",
                                 "rent_event_id": "r11",
                                 "late_event_id": None},
               "65@g9_2026-12": {"past_due": 0.0, "calendar_id": "calA",
                                 "rent_event_id": "r12",
                                 "late_event_id": None}}, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch35 = SyncOrchestrator()
os.unlink(tmp)
with mock.patch.object(orch35.gcal, "delete_event") as del35:
    orch35._prune_beyond_horizon("65@g9", "calA", "2026-09", "2026-08")
check("beyond-horizon placeholder pruned (event + entry)",
      del35.call_args_list == [mock.call("calA", "r12")]
      and orch35.state.get("65@g9", "2026-12") is None)
check("kickstart / other-calendar / in-horizon entries untouched",
      orch35.state.get("65@g9", "2026-09") is not None
      and orch35.state.get("65@g9", "2026-10") is not None
      and orch35.state.get("65@g9", "2026-11") is not None)

print("\n=== 36. Departed occupancies: flag → dual-confirm → clean ===")
check("DEPARTED_MAX_PER_RUN default is 10", _cfg.DEPARTED_MAX_PER_RUN == 10)
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as f:
    json.dump({
        "_commitments": {"41@g2": [
            {"event_id": "prom41", "anchor_date": "2026-08-20",
             "source_type": "status", "origin_month": "2026-08",
             "calendar_id": "calB", "covers_rent_month": "2026-08"}]},
        "_calendars": {"g2": "calB"},
        "41@g2_2026-06": {"status": "✅ Paid", "past_due": 0.0,
                          "calendar_id": "calB", "status_event_id": "sJun",
                          "collapse_state": "collapsed",
                          "payment_event_ids": [], "late_event_id": None},
        "41@g2_2026-07": {"status": "🔴 Unpaid", "past_due": 9090.0,
                          "calendar_id": "calB", "status_event_id": "sJul",
                          "collapse_state": None,
                          "payment_event_ids": ["pJul"],
                          "late_event_id": None},
        "41@g2_2026-09": {"status": "🔴 Unpaid", "past_due": 0.0,
                          "calendar_id": "calB", "rent_event_id": "rSep",
                          "late_event_id": None},
    }, f)
    tmp = f.name
with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info"), \
     mock.patch("googleapiclient.discovery.build"), \
     mock.patch.object(state, "STATE_FILE", Path(tmp)):
    orch36 = SyncOrchestrator()
os.unlink(tmp)
_t36 = _date(2026, 8, 16)
prev36 = [{"occupancy_id": 41, "tenant": "Johnson, Eric", "unit": "1",
           "unit_id": 7, "property_name": "3858 W Jackson",
           "property_street": "3858 W Jackson", "property_city": "Chicago",
           "property_state": "IL", "property_zip": "60624",
           "move_out": None}]
roll36 = [{"occupancy_id": None, "status": "Vacant-Unrented", "unit_id": 7,
           "last_move_out": "2026-07-31"}]

orch36._flag_departures(roll36, prev36, _t36)
p36 = orch36.state.data["_departed_pending"].get("41")
check("vanished oid flagged with last row + unit_id + backlog",
      p36 is not None and p36["unit_id"] == 7
      and p36["last_row"]["tenant"] == "Johnson, Eric"
      and p36["backlog"] is True and p36["scopes"] == ["g2"])

# still in tenant_directory → dual-report blocks the cleanup
with mock.patch.object(orch36.gcal, "delete_event") as del36a:
    orch36._clean_departed(roll36, [{"occupancy_id": 41}], _t36)
check("dual-report: oid still in tenant_directory -> untouched",
      not del36a.called
      and "41" in orch36.state.data["_departed_pending"])

# absent from both reports → full cleanup
ins36 = orch36.gcal.service.events.return_value.insert
ins36.return_value.execute.return_value = {"id": "mk41"}
ins36.reset_mock()
with mock.patch.object(orch36.gcal, "delete_event") as del36, \
     mock.patch.object(orch36.gcal, "find_all_events_by_type",
                       return_value=[]) as fae36:
    orch36._clean_departed(roll36, [], _t36)
deleted36 = {c.args[1] for c in del36.call_args_list}
marker36 = ins36.call_args.kwargs["body"]
audit36 = orch36.state.data["_departed"].get("41")
check("cleanup deletes promises, unsettled status, payments, placeholders",
      deleted36 == {"prom41", "sJul", "pJul", "rSep"})
check("settled month's event kept", "sJun" not in deleted36
      and audit36 and audit36["events_kept"] == 1)
check("marker: 📦, no dollar figure, AppFolio pointer, move-out from "
      "vacant-row join",
      marker36["summary"].startswith("📦 · Eric Johnson · Unit 1 · "
                                     "3858 W Jackson")
      and "$" not in marker36["summary"] + marker36["description"]
      and "Final ledger lives in AppFolio." in marker36["description"]
      and "Moved out:    Jul 31, 2026" in marker36["description"]
      and marker36["start"] == {"date": "2026-07-31"}
      and marker36["end"] == {"date": "2026-08-01"}
      and marker36["extendedProperties"]["private"]["okpm_event_type"]
      == "moved_out")
check("state purged + audit recorded + pending cleared + backlog marker",
      orch36.state.scoped_months("41@g2") == []
      and "41" not in orch36.state.data["_departed_pending"]
      and audit36["move_out_date"] == "2026-07-31"
      and audit36["months_purged"] == 3
      and orch36.state.migration_done("departed_backlog_v1"))

# reappearance BEFORE cleanup clears the flag
orch36.state.data["_departed_pending"]["55"] = {
    "first_missing_at": "2026-08-15", "scopes": ["g2"], "backlog": False}
orch36._flag_departures(
    [{"occupancy_id": 55, "status": "Current"}], None, _t36)
check("reappeared pending oid -> flag cleared",
      "55" not in orch36.state.data["_departed_pending"])

# reappearance AFTER cleanup removes the markers + audit
with mock.patch.object(orch36.gcal, "delete_event") as del36c, \
     mock.patch.object(orch36.gcal, "find_all_events_by_type",
                       return_value=[{"id": "mk41b"}]):
    orch36._clean_departed(
        [{"occupancy_id": 41, "status": "Current"}], [], _t36)
check("post-clean reappearance: markers removed, audit dropped",
      {c.args[1] for c in del36c.call_args_list} >= {"mk41", "mk41b"}
      and "41" not in orch36.state.data["_departed"])

# circuit breaker: too many NON-backlog confirmables defer entirely
with mock.patch.object(_orch_mod, "DEPARTED_MAX_PER_RUN", 2):
    for i in (201, 202, 203):
        orch36.state.data[f"{i}@g2_2026-07"] = {
            "past_due": 1.0, "calendar_id": "calB",
            "status_event_id": f"s{i}", "late_event_id": None}
        orch36.state.data["_departed_pending"][str(i)] = {
            "first_missing_at": "2026-08-15", "scopes": ["g2"],
            "backlog": False, "last_row": None, "unit_id": None}
    with mock.patch.object(orch36.gcal, "delete_event") as del36d:
        orch36._clean_departed([], [], _t36)
    check("mass-vanish breaker defers cleanup (3 > 2)",
          not del36d.called
          and all(str(i) in orch36.state.data["_departed_pending"]
                  for i in (201, 202, 203)))

# resumability: a missing calendar id keeps the oid pending, state intact
orch36.state.data["_departed_pending"] = {
    "301": {"first_missing_at": "2026-08-15", "scopes": ["g7"],
            "backlog": False, "last_row": None, "unit_id": None}}
orch36.state.data["301@g7_2026-07"] = {
    "past_due": 5.0, "calendar_id": "calMissing",
    "status_event_id": "s301", "late_event_id": None}
with mock.patch.object(orch36.gcal, "delete_event") as del36e:
    orch36._clean_departed([], [], _t36)
check("missing calendar id -> cleanup fails safe (pending + state kept)",
      "301" in orch36.state.data["_departed_pending"]
      and orch36.state.get("301@g7", "2026-07") is not None
      and not del36e.called)

print()
if failures:
    print(f"❌ {len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("✅ All smoke-test checks passed. Package is wired correctly and all four fixes behave.")
