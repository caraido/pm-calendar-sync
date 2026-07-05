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

# The q-scan's client-side filter: tagged / untagged-with-divider / plain.
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
found = orch4.gcal.find_untagged_commitment_copies("calA")
check("q-scan keeps only untagged divider events",
      [e["id"] for e in found] == ["u1"])

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
with mock.patch.object(orch4.gcal, "find_untagged_commitment_copies",
                       return_value=[copy_ev]), \
     mock.patch.object(orch4, "_mirror_commitment_to_siblings") as mir:
    n = orch4._adopt_untagged_commitments(
        [(row_fx, {"owner_id": 9})], 9, "calA", {}, {}, _date(2026, 7, 4))
body_sent = upd.call_args.kwargs["body"]
check("adoption re-tags the event in place",
      n == 1 and upd.called
      and body_sent["extendedProperties"]["private"]["okpm_event_type"] == "commitment"
      and body_sent["extendedProperties"]["private"]["okpm_occupancy_id"] == "99")
comms = orch4.state.get_commitments("99@9")
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
with mock.patch.object(orch4.gcal, "find_untagged_commitment_copies",
                       return_value=[copy_ev]):
    n2 = orch4._adopt_untagged_commitments(
        [(other_row, {})], 9, "calA", {}, {}, _date(2026, 7, 4))
check("unmatched tenant -> skipped, no writes", n2 == 0 and not upd.called)

twin = {**row_fx, "occupancy_id": 98}   # same tenant, same address/unit
with mock.patch.object(orch4.gcal, "find_untagged_commitment_copies",
                       return_value=[copy_ev]):
    n3 = orch4._adopt_untagged_commitments(
        [(row_fx, {}), (twin, {})], 9, "calA", {}, {}, _date(2026, 7, 4))
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
                                   total_payments=2, month_fully_paid=True)
check("settled month -> muting wins over NSF red", b2["colorId"] == "8")
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
            "description": "Method:       ACH (#1A4A-5A70)\nAmount:       $1,530.00\nStatus:       ✅ Paid"},
}
unit85 = {"occupancy_id": "85", "tenant": "Darden, Sanquia",
          "additional_tenants": "", "rent": 1500.0, "past_due": 3030.0}
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
      and bodies7["p71"]["extendedProperties"]["private"]["okpm_nsf"] == "1")
check("non-matching surplus event deleted (positional duplicate)",
      dele5.call_args == mock.call("calA", "p72"))
july = orch5.state.get("85@10", "2026-07")
check("flipped event recorded in nsf_event_ids + marker",
      july.get("nsf_event_ids") == ["p71"]
      and "CCCC-DDDD" in [r["key"] for r in july["nsf_reversals_applied"]])
check("vanished single payment matched via stored payments -> noted",
      "EEEE-FFFF" in [r["key"] for r in july["nsf_reversals_applied"]]
      and "REVERSED (NSF)" in bodies7["st7"]["description"])
check("62-day-old reversal ignored (no marker)",
      "OLD1-OLD1" not in [r["key"] for r in july["nsf_reversals_applied"]])

print()
if failures:
    print(f"❌ {len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("✅ All smoke-test checks passed. Package is wired correctly and all four fixes behave.")
