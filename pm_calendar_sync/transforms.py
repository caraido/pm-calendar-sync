"""Pure data transforms and value helpers (no network or calendar I/O)."""
import re
from datetime import date, timedelta
from typing import Optional

from .config import COMMITMENT_DIVIDER, LATE_GRACE_DAYS


_MONTH_NAMES = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

def normalize_tenant_name(name: str) -> str:
    """'Last, First' → 'First Last'"""
    name = (name or "").strip()
    if "," in name:
        parts = name.split(",", 1)
        return f"{parts[1].strip()} {parts[0].strip()}"
    return name


def detect_intended_month(desc: str, payment_date: str) -> Optional[tuple]:
    match = re.search(r"\b(" + "|".join(_MONTH_NAMES) + r")\s+rent\b", desc.lower())
    if not match: return None
    intended = _MONTH_NAMES[match.group(1)]
    try: pay = date.fromisoformat(payment_date)
    except ValueError: return None
    if intended == pay.month: return None
    year = pay.year - (1 if intended > pay.month + 1 else 0)
    return (year, intended)


def _shorten_desc(desc: str) -> str:
    desc = re.sub(r'ACH Payment \(Reference (#[\w-]+)\)', r'ACH (\1)', desc)
    desc = re.sub(r'Credit Card Payment \(Reference (#[\w-]+)\)', r'Credit Card (\1)', desc)
    desc = re.sub(r'Payment \(Reference #(\w+)\)\s*', r'\1 - ', desc)
    return desc[:80].strip(" -")


# Reverse of the Source display map in calendar_manager._build_commitment_event.
# Raw source keys pass through so an unedited auto section always round-trips.
_SOURCE_DISPLAY_TO_TYPE = {
    "Kickstart (future rent)": "kickstart",
    "Preview/late (arrears)":  "late",
    "Status event (dragged)":  "status",
    "Payment event (dragged)": "payment",
    "kickstart": "kickstart",
    "late":      "late",
    "status":    "status",
    "payment":   "payment",
}


def parse_commitment_auto_section(description: str) -> Optional[dict]:
    """
    Best-effort parse of a commitment event body — used to ADOPT PM
    copy-paste copies, which lose their okpm extended properties (the
    Calendar UI does not copy extendedProperties.private).

    Returns None when COMMITMENT_DIVIDER is absent; else
      pm_notes      text above the divider (PM-editable, preserved verbatim)
      tenant        normalized tenant from the auto section's 'Tenant:' line
                    (None when the line is missing/mangled)
      source_type   mapped from the 'Source:' line; defaults to "status" —
                    promise-typed semantics (≥1-promise, resolve-on-paid,
                    covers the current month), matching the dominant origin
                    of split copies (a dragged status promise)
      auto_section  the raw text below the divider (for address checks)
    """
    if COMMITMENT_DIVIDER not in (description or ""):
        return None
    pm_notes, auto_section = description.split(COMMITMENT_DIVIDER, 1)
    tenant = None
    m = re.search(r"^Tenant:\s+(.+)$", auto_section, re.M)
    if m:
        tenant = normalize_tenant_name(m.group(1).strip())
    source_type = "status"
    m = re.search(r"^Source:\s+(.+)$", auto_section, re.M)
    if m:
        source_type = _SOURCE_DISPLAY_TO_TYPE.get(m.group(1).strip(), "status")
    return {
        "pm_notes":     pm_notes.rstrip(),
        "tenant":       tenant,
        "source_type":  source_type,
        "auto_section": auto_section,
    }


def build_owner_property_map(owners: list[dict]) -> dict:
    """Maps property_id → list of owners (supports co-ownership)."""
    m: dict[int, list[dict]] = {}
    for o in owners:
        for pid in (o.get("properties_owned_i_ds") or "").split(","):
            if pid.strip().isdigit():
                m.setdefault(int(pid.strip()), []).append(o)
    return m


def build_tenant_info_map(tenants: list[dict]) -> dict:
    m = {}
    for t in tenants:
        if t.get("primary_tenant") != "Yes": continue
        oid = t.get("occupancy_id")
        if not oid: continue
        raw_phone  = (t.get("phone_numbers") or "").strip()
        phone      = raw_phone.replace("Phone:","").replace("Mobile:","").replace("Fax:","").strip()
        fee_type   = (t.get("late_fee_type") or "").strip()
        fee_base   = float(t.get("late_fee_base_amount") or 0)
        fee_daily  = float(t.get("late_fee_daily_amount") or 0)
        grace_days = int(t.get("rent_grace_days") or LATE_GRACE_DAYS)
        if fee_type == "Flat Fee":
            fee_desc = f"Flat ${fee_base:,.2f} after {grace_days} days"
        elif fee_daily > 0:
            fee_desc = f"${fee_base:,.2f} + ${fee_daily:,.2f}/day after {grace_days} days"
        elif fee_base > 0:
            fee_desc = f"${fee_base:,.2f} after {grace_days} days"
        else:
            fee_desc = f"No late fee ({grace_days} days grace)"
        m[int(oid)] = {"phone": phone or "N/A", "late_fee_desc": fee_desc, "grace_days": grace_days}
    return m


def build_payment_map(ledger_rows: list[dict]) -> dict:
    payments = {}
    for row in ledger_rows:
        try: amount = float(row.get("credit") or 0)
        except: continue
        if amount <= 0: continue
        payer    = normalize_tenant_name(row.get("payer") or "Unknown")
        desc     = (row.get("description") or "").strip()
        raw_date = row.get("date", "")
        is_nsf   = "nsf" in desc.lower() or "reversed" in desc.lower()
        payments.setdefault(payer, []).append({
            "date":           raw_date,
            "amount":         amount,
            "description":    _shorten_desc(desc),
            "is_nsf":         is_nsf,
            "intended_month": detect_intended_month(desc, raw_date),
        })
    return payments


def build_reversal_map(ledger_rows: list[dict]) -> dict:
    """
    payer(normalized) → [reversal records] for NEGATIVE-credit ledger rows.

    NSF reversals arrive as separate rows with credit < 0 (live example:
    description 'NSF reversal receipt for Reference #1A4A-5A70',
    credit "-1530.00", dated on the BOUNCE day, not the payment day) —
    build_payment_map's `amount <= 0` gate discards them, which is exactly
    why reversed payments used to vanish without a trace.  Record shape:
      {date, amount (positive), ref (token after '#', or None), description}
    Non-NSF negative adjustments are collected too — they simply never match
    one of our payment events and age out silently.
    """
    reversals: dict = {}
    for row in ledger_rows:
        try:
            amount = float(row.get("credit") or 0)
        except (TypeError, ValueError):
            continue
        if amount >= 0:
            continue
        desc  = (row.get("description") or "").strip()
        m     = re.search(r"#([\w-]+)", desc)
        payer = normalize_tenant_name(row.get("payer") or "Unknown")
        reversals.setdefault(payer, []).append({
            "date":        row.get("date", ""),
            "amount":      abs(amount),
            "ref":         m.group(1) if m else None,
            "description": desc,
        })
    return reversals


def parse_status_line(description: str) -> str:
    """The event's own 'Status:' line value ('' when absent) — used to
    un-grey settled-muted events back to their true colors after a
    reversal breaks the month's settlement."""
    m = re.search(r"^Status:\s+(.+)$", description or "", re.M)
    return m.group(1).strip() if m else ""


def diff_rent_roll(cached_rows: Optional[list[dict]], fresh_rows: list[dict],
                   eps: float = 0.005) -> set:
    """
    Bare occupancy_ids (str) whose money moved since the cached snapshot —
    the unit scope for update mode.  Only status=="Current" rows compared.

    Changed = |past_due delta| > eps, rent changed (> eps), or the oid is
    absent from the snapshot (new lease).  cached_rows=None → every fresh
    Current oid (bootstrap: no baseline yet).

    Values are float-coerced the same way _make_unit coerces them, so None /
    string variance in the raw report can never false-positive.
    """
    def _f(v) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    fresh_current = [r for r in fresh_rows if r.get("status") == "Current"]
    if cached_rows is None:
        return {str(r.get("occupancy_id")) for r in fresh_current}

    old = {str(r.get("occupancy_id")): r
           for r in cached_rows if r.get("status") == "Current"}
    changed = set()
    for r in fresh_current:
        oid  = str(r.get("occupancy_id"))
        prev = old.get(oid)
        if prev is None:
            changed.add(oid)  # new lease (or newly Current)
        elif (abs(_f(r.get("past_due")) - _f(prev.get("past_due"))) > eps
              or abs(_f(r.get("rent")) - _f(prev.get("rent"))) > eps):
            changed.add(oid)
    return changed


def compute_running_balances(sorted_payments: list[dict], current_past_due: float) -> list[float]:
    balances = []
    for i, p in enumerate(sorted_payments):
        subsequent = sum(pp["amount"] for pp in sorted_payments[i+1:] if not pp["is_nsf"])
        balances.append(current_past_due + subsequent)
    return balances


def format_address(row: dict) -> str:
    return ", ".join(p for p in [
        row.get("property_street",""), row.get("property_city",""),
        row.get("property_state",""), row.get("property_zip","") or "",
    ] if p)


def unit_label(row: dict) -> str:
    raw = (row.get("unit") or "").strip()
    if not raw:
        return ""
    # AppFolio is inconsistent: some properties store "Unit 2", others store a
    # bare "2". Normalize so every label reads "Unit X" (and never "Unit Unit").
    return raw if raw.lower().startswith("unit") else f"Unit {raw}"


def owner_display_name(owner: dict) -> str:
    name = (owner.get("name") or "").strip()
    if name: return name
    return f"{(owner.get('first_name') or '').strip()} {(owner.get('last_name') or '').strip()}".strip() or "Unknown Owner"


def _next_day(iso_date: str) -> str:
    """
    Return the day AFTER the given ISO date (YYYY-MM-DD).

    Google Calendar all-day events use an EXCLUSIVE end date: a one-day event
    on date D must have start=D and end=D+1.  Writing end==start creates a
    zero-length span that the Calendar UI rejects the moment you try to edit
    the event ("the event end time cannot be set before the start time").
    All event builders use this so every all-day event is a proper 1-day span.
    """
    return (date.fromisoformat(iso_date) + timedelta(days=1)).isoformat()
