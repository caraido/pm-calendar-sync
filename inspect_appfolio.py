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

DB   = os.environ["APPFOLIO_DB_NAME"]
CID  = os.environ["APPFOLIO_CLIENT_ID"]
CSC  = os.environ["APPFOLIO_CLIENT_SECRET"]
BASE = f"https://{DB}.appfolio.com/api/v1"

s = requests.Session()
s.auth = (CID, CSC)
s.headers["Accept"] = "application/json"

def dump(endpoint, params=None):
    r = s.get(f"{BASE}/{endpoint}", params=params)
    print(f"\n{'='*60}")
    print(f"GET {endpoint} — status {r.status_code}")
    print('='*60)
    try:
        body = r.json()
        results = body.get("results", body)
        if isinstance(results, list) and results:
            print("First result keys:", list(results[0].keys()))
            print(json.dumps(results[0], indent=2, default=str))
        else:
            print(json.dumps(body, indent=2, default=str))
    except Exception:
        print(r.text[:500])

# Inspect each endpoint sync.py depends on
dump("leases", {"status": "current"})
dump("payments", {"from_date": "2026-05-01"})
