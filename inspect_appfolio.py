"""
inspect_appfolio.py
───────────────────
Run this ONCE locally to print raw AppFolio API responses.
Use it to verify field names before running sync.py.

Usage:
    APPFOLIO_DB_NAME=okpm \
    APPFOLIO_CLIENT_ID=xxx \
    APPFOLIO_CLIENT_SECRET=xxx \
    python inspect_appfolio.py
"""

import os, json, requests
from datetime import date

DB   = os.environ["APPFOLIO_DB_NAME"]
CID  = os.environ["APPFOLIO_CLIENT_ID"]
CSC  = os.environ["APPFOLIO_CLIENT_SECRET"]
BASE = f"https://{DB}.appfolio.com/api/v1"

# Credentials go in the URL for Basic Auth (AppFolio v2 style)
BASE = f"https://{CID}:{CSC}@{DB}.appfolio.com/api/v2/reports"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

today      = date.today()
month_start = today.replace(day=1).isoformat()
month_end   = today.isoformat()
 
# ── 1. Probe owner-related reports ──────────────────────────────────────────
OWNER_REPORTS = [
    ("owner_directory",         {}),
    ("owner_contact_info",      {}),
    ("owners",                  {}),
    ("property_owners",         {}),
    ("owner_detail",            {}),
    ("owner_list",              {}),
    ("property_groups",         {}),   # might map group_id → owner
    ("portfolio_summary",       {}),
    ("owner_statement_summary", {"from_date": month_start, "to_date": month_end}),
    ("cash_flow",               {"from_date": month_start, "to_date": month_end}),
]
 
# ── 2. Probe tenant_ledger filtered by occupancy_id ─────────────────────────
# Using occupancy_id=101 from the first rent_roll row
OCCUPANCY_ID = 101
LEDGER_PAYLOADS = [
    # Try different filter key names — AppFolio is inconsistent
    {"occupancy_id": OCCUPANCY_ID,  "from_date": month_start, "to_date": month_end},
    {"occupancy_ids": [OCCUPANCY_ID], "from_date": month_start, "to_date": month_end},
    {"id": OCCUPANCY_ID,            "from_date": month_start, "to_date": month_end},
]
 
# ── 3. Retry the rate-limited ones ──────────────────────────────────────────
RETRY_REPORTS = [
    ("receivable_aging", {}),
    ("delinquency",      {}),
    ("unpaid_charges",   {}),
]
 
 
def probe(report_name, payload, label=None):
    url = f"{BASE}/{report_name}.json"
    r = requests.post(url, headers=HEADERS, json=payload, timeout=15)
    tag = label or report_name
    print(f"\n{'='*60}")
    print(f"POST /api/v2/reports/{tag}  →  HTTP {r.status_code}")
    print(f"Payload: {json.dumps(payload)}")
    print('='*60)
 
    if r.status_code == 200:
        try:
            body = r.json()
            results = body.get("results", body)
            if isinstance(results, list) and results:
                print(f"✅  {len(results)} rows  |  Keys: {list(results[0].keys())}")
                print(json.dumps(results[0], indent=4, default=str))
            else:
                print("✅  200 OK — empty results or unexpected shape")
                print(json.dumps(body, indent=4, default=str)[:600])
        except Exception as e:
            print(f"⚠️  Parse error: {e}\n{r.text[:400]}")
    elif r.status_code == 400:
        print(f"❌  400 — {r.text[:200]}")
    elif r.status_code == 404:
        print("❌  404 — report not found")
    elif r.status_code == 422:
        print(f"⚠️  422 — payload rejected: {r.text[:200]}")
    elif r.status_code == 429:
        print("⏳  429 — still rate limited, retry in a few minutes")
    elif r.status_code == 401:
        print("🔑  401 — auth failed")
    else:
        print(f"⚠️  {r.status_code}: {r.text[:200]}")
 
 
print(f"\n{'#'*60}")
print("PART 1 — Owner-related reports")
print(f"{'#'*60}")
for name, payload in OWNER_REPORTS:
    probe(name, payload)
 
print(f"\n{'#'*60}")
print(f"PART 2 — tenant_ledger filtered by occupancy_id={OCCUPANCY_ID}")
print(f"{'#'*60}")
for payload in LEDGER_PAYLOADS:
    probe("tenant_ledger", payload, label=f"tenant_ledger {list(payload.keys())}")
 
print(f"\n{'#'*60}")
print("PART 3 — Retry rate-limited reports")
print(f"{'#'*60}")
for name, payload in RETRY_REPORTS:
    probe(name, payload)
 
print("\n\nDone. Paste the full output and I'll finalize sync.py.")