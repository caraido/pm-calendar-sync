"""Status classification and colour / emoji mapping."""
from .config import (
    COLOR_PAID, COLOR_PREPAID, COLOR_PARTIAL,
    COLOR_UNPAID, COLOR_LATE,
)


STATUS_PAID    = "✅ Paid"
STATUS_PREPAID = "🩷 Prepaid"
STATUS_PARTIAL = "🟡 Partial"
STATUS_UNPAID  = "🔴 Unpaid"
STATUS_LATE    = "🔴 Late"


def classify_status(rent: float, past_due: float) -> str:
    if past_due < 0:      return STATUS_PREPAID
    elif past_due == 0:   return STATUS_PAID
    elif past_due < rent: return STATUS_PARTIAL
    else:                 return STATUS_UNPAID


def payment_status(rent: float, balance: float) -> str:
    """
    Status for any event that represents a RECEIVED payment (the migrated
    status event on a payment date, or an additional-payment event).

    Differs from classify_status in one way: a tenant who has paid something
    this month is NEVER shown 🔴 Unpaid, even when the remaining balance is
    one month's rent or more (i.e. they're in arrears across months).  As long
    as a balance remains it reads 🟡 Partial.  Zero → ✅ Paid, credit → 🩷 Prepaid.

    Rationale: 🔴 means "nothing received"; once money has come in this month
    the event should reflect a partial payment, not a missed one.
    """
    if balance < 0:    return STATUS_PREPAID
    elif balance == 0: return STATUS_PAID
    else:              return STATUS_PARTIAL


def color_for_status(status: str) -> str:
    return {
        STATUS_PAID:    COLOR_PAID,
        STATUS_PREPAID: COLOR_PREPAID,
        STATUS_PARTIAL: COLOR_PARTIAL,
        STATUS_UNPAID:  COLOR_UNPAID,
        STATUS_LATE:    COLOR_LATE,
    }.get(status, COLOR_UNPAID)


def emoji_for_status(status: str) -> str:
    return {
        STATUS_PAID:    "✅",
        STATUS_PREPAID: "🩷",
        STATUS_PARTIAL: "🟡",
        STATUS_UNPAID:  "🔴",
        STATUS_LATE:    "🔴",
    }.get(status, "🔴")
