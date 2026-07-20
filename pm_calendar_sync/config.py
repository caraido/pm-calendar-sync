"""Configuration: environment variables, shared constants, and logger.

Importing this module reads the required AppFolio / Google environment
variables — same behaviour as the original sync.py (the process must have
them set to run).
"""
import os
import logging
from pathlib import Path
from urllib.parse import quote

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pm_calendar_sync")


APPFOLIO_DB_NAME       = os.environ["APPFOLIO_DB_NAME"].strip()
APPFOLIO_CLIENT_ID     = os.environ["APPFOLIO_CLIENT_ID"].strip()
APPFOLIO_CLIENT_SECRET = os.environ["APPFOLIO_CLIENT_SECRET"].strip()
GOOGLE_SA_JSON         = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GOOGLE_SCOPES          = ["https://www.googleapis.com/auth/calendar"]

LATE_GRACE_DAYS             = int(os.environ.get("LATE_GRACE_DAYS", 5))
RENT_DUE_DAY                = int(os.environ.get("RENT_DUE_DAY", 1))
PM_EMAIL                    = os.environ.get("PM_EMAIL", "")
DEFAULT_LEASE_MONTHS        = int(os.environ.get("DEFAULT_LEASE_MONTHS", 12))
FORCE_REFRESH               = os.environ.get("FORCE_REFRESH", "").lower() == "true"
COMMITMENT_LOOKAHEAD_MONTHS = int(os.environ.get("COMMITMENT_LOOKAHEAD_MONTHS", 3))
TIMEZONE                    = os.environ.get("TIMEZONE", "America/Chicago")

STATE_FILE       = Path("state.json")
CALENDAR_PREFIX  = "OKPM"
RETIRED_PREFIX   = "[RETIRED] "   # prepended to old owner calendars at group cutover
AF_API_DELAY_SEC = 2.0

# Credentials are URL-encoded so special characters (@ : / # etc.) in the
# client id/secret can't corrupt the embedded-credential URL and cause a 403.
_AF_BASE    = (f"https://{quote(APPFOLIO_CLIENT_ID, safe='')}:"
               f"{quote(APPFOLIO_CLIENT_SECRET, safe='')}"
               f"@{APPFOLIO_DB_NAME}.appfolio.com/api/v2/reports")
_AF_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# Divider between PM-editable notes and auto-generated section in commitments
COMMITMENT_DIVIDER = "─" * 16 + " AUTO-SYNCED — do not edit below " + "─" * 16

# Google Calendar color IDs
COLOR_PAID       = "2"   # sage green
COLOR_PREPAID    = "4"   # flamingo pink
COLOR_PARTIAL    = "5"   # banana yellow
COLOR_UNPAID     = "11"  # tomato red
COLOR_LATE       = "11"  # tomato red
COLOR_SETTLED    = "8"   # graphite — muted grey for earlier payments once paid in full
# COLOR_COMMITMENT (tangerine, "6") was the old ⚠️ Overdue colour. The overdue
# state was removed — commitments now take their balance colour (red/yellow) —
# so this is no longer applied. Kept here for reference / possible future use.
COLOR_COMMITMENT = "6"   # tangerine — unused since overdue removal

GCAL_RETRY_ATTEMPTS = 3
GCAL_RETRY_BASE_DELAY = 5   # seconds — doubled on each retry
