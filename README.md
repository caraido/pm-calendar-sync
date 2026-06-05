# 📅 OKPM Calendar Sync

> **Your entire rental portfolio, live in Google Calendar — zero servers, zero spreadsheets, zero chasing.**

OKPM Calendar Sync automatically pulls real-time rent and payment data from AppFolio and turns it into beautiful, color-coded Google Calendars — one per property owner. The PM gets a full interactive collections dashboard. Owners get a read-only window into their portfolio. Everyone always knows exactly who owes what, right now.

**Runs free on GitHub Actions. No server to maintain. No database to manage. Just plug in your credentials and go.**

---

## ✨ Why you'll love it

| | Feature | What it means for you |
|---|---|---|
| 🔄 | **Auto-syncs every 30 min** | Payment data is always fresh — up to every 15 min during rent week |
| 📆 | **One calendar per owner** | Each owner sees only their portfolio, beautifully organized |
| 🎨 | **Instant visual status** | Color-coded events tell the whole story at a glance |
| 👆 | **Drag to track promises** | Log a promise-to-pay in seconds — just drag an event to the date |
| 🤝 | **Split payment plans** | Copy-paste a commitment to map out installment arrangements |
| 🔍 | **Daily "who owes me" view** | Open today's date for an instant collections dashboard |
| 🏘️ | **Co-ownership support** | One unit, multiple owners — each gets their own calendar entry |
| 💸 | **$0 infrastructure cost** | Runs entirely on free GitHub Actions |

---

## 📋 Table of contents

* [How it works](#how-it-works)
* [The calendars](#the-calendars)
* [The visual language](#the-visual-language-status-model)
* [Event types](#event-types)
* [The monthly lifecycle](#the-monthly-lifecycle)
* [Promise-to-pay](#promise-to-pay-commitments)
* [The "today" dashboard](#the-today-dashboard)
* [Setup](#setup)
* [Configuration reference](#configuration-reference)
* [Scheduling](#scheduling)
* [State file](#state-file)
* [Helper scripts](#helper-scripts)
* [AppFolio API notes](#appfolio-api-notes)
* [Operations & troubleshooting](#operations--troubleshooting)
* [Project structure](#project-structure)

---

## 🔄 How it works

```
┌─────────────────────┐        ┌──────────────────────┐        ┌─────────────────────┐
│   AppFolio Plus     │        │  sync.py             │        │  Google Calendar    │
│  (v2 Reports API)   │──────► │  (GitHub Actions)    │──────► │  (one cal / owner)  │
│                     │        │                      │        │                     │
│  📋 rent_roll       │        │  ✅ builds status,   │        │  👁️ owners subscribe │
│  👤 owner_dir       │        │  💳 payment, today,  │        │     (read-only)     │
│  🧑 tenant_dir      │        │  📌 placeholder &    │        │  🖱️ PM owns + drags  │
│  📒 tenant_ledger   │        │     commitment events│        │     events to manage│
└─────────────────────┘        └──────────────────────┘        └─────────────────────┘
```

Each run, the sync engine:

1. 📥 **Pulls four AppFolio reports** — `rent_roll`, `owner_directory`, `tenant_directory`, and the current month's `tenant_ledger`.
2. 🔗 **Maps every active lease to its owner(s)** — supports co-ownership so a unit can appear on multiple owners' calendars simultaneously.
3. 📆 **Finds or creates each owner's calendar** — ensures access permissions are correct, then syncs each of their units.
4. ✏️ **Writes / updates / removes calendar events** — reflecting each unit's live payment situation.
5. 💾 **Saves `state.json`** and commits it back to the repo automatically.

No server. No database. The only moving parts are the GitHub Actions workflow, the Python script, and the committed state file.

---

## 📆 The calendars

Each owner gets **one dedicated calendar** named `"{Owner Name} Portfolio"` (e.g., `Ryan Palmer Portfolio`) — everything neatly separated, nothing mixed together.

```
┌────────────────────────────────────────────────────┐
│  👑 Service account  ──► owns all calendars        │
│  🏢 PM account       ──► full owner access         │
│                           (drag, edit, manage)      │
│  👤 Property owners  ──► read-only view            │
│                           (their portfolio only)    │
└────────────────────────────────────────────────────┘
```

> 💡 **Automatic legacy rename:** If you're upgrading from the old `OKPM · … Portfolio` naming, the first run renames calendars in place — preserving all events, sharing settings, and IDs. No duplicates, no data loss.

> 📬 **Sidebar tip:** Sharing grants access, but Google doesn't always auto-add a calendar to the sidebar. Run `share_portfolio_calendars.py` once to trigger "Add this calendar" emails for each owner.

---

## 🎨 The visual language (status model)

No more digging through spreadsheets. Every event has a leading emoji and a matching Google Calendar color — the PM can read an entire month in seconds.

```
   🩷 Prepaid  ──────────────────────── flamingo pink  (credit balance)
   ✅ Paid     ──────────────────────── sage green     (balance = $0)
   🟡 Partial  ──────────────────────── banana yellow  (partial payment)
   🔴 Unpaid   ──────────────────────── tomato red     (full month owed)
   ⚠️ Overdue  ──────────────────────── tangerine      (missed promise)
```

| Status | Condition | Color |
| ------ | --------- | ----- |
| 🩷 **Prepaid** | Credit / overpaid (`past_due < 0`) | Flamingo pink |
| ✅ **Paid** | Fully paid (`past_due == 0`) | Sage green |
| 🟡 **Partial** | Partial payment (`0 < past_due < rent`) | Banana yellow |
| 🔴 **Unpaid** | Full month owed (`past_due >= rent`) | Tomato red |
| ⚠️ **Overdue** | Missed promise, still unpaid | Tangerine |

The live `past_due` balance from AppFolio is the single source of truth — including negative values (credits), which trigger the 🩷 Prepaid state automatically.

---

## 📌 Event types

Five smart event types work together to tell the complete story of every unit's payment history and future:

| Type | 📍 Appears on | Purpose |
| ---- | ------------- | ------- |
| `status` | 1st of month → migrates to first payment date | The month's headline status for a unit |
| `payment` | Each payment date after the first | Logs every additional payment individually |
| `late` | Today's date | The **daily collections dashboard** marker |
| `rent` | 1st of future months | Frozen placeholder for upcoming rent |
| `commitment` | A promised payment date | Tracks a promise-to-pay the PM is managing |

### 🖱️ Drag to manage — movable vs. locked

The PM's primary interaction is **dragging events** on the calendar:

- **Movable** — drag any of these to a future date to instantly create a promise-to-pay:
  - Future-month placeholders (`rent`)
  - Today markers (`late`)
  - Status events (`status`)
  - Payment events (`payment`)
- **Locked** — status events that haven't been dragged snap back to their correct date on the next sync, protecting against accidental moves.

---

## 📅 The monthly lifecycle

### This month — always up to date

```
  Day 1      First payment       Additional payments
    │              │                    │     │
    ▼              ▼                    ▼     ▼
  🔴 Unpaid  ──► ✅ Paid          💳 +$500  💳 +$200
  (on the 1st)  (migrates here)   (each gets its own event)
```

- **Status event** starts on the 1st showing the amount due. When the first payment arrives, it **migrates** to that payment's date and absorbs it — now showing the amount paid and the running balance.
- **Additional payments** each get their own `payment` event with the running balance after that payment.
- **NSF / reversed payments** are auto-detected and flagged ⚠️ REVERSED — they never count toward the paid total.

### Future months — always planned ahead

- **Frozen placeholders** appear on the 1st of every upcoming month through the lease end — a visual forward view of expected income.
- **Credit rollover** — if a tenant has a credit balance right now, next month's placeholder automatically reflects that credit against next month's rent.

---

## 🤝 Promise-to-pay (commitments)

This is where OKPM Calendar Sync really shines as a collections tool. When a tenant commits to paying on a future date, the PM **drags** the relevant event to that date. That's it — the sync handles the rest automatically.

```
   PM drags event             Next sync run             Promise resolved
   to future date    ──────►  detects the move  ──────►  when paid in full
        │                     converts to                 (all promises
        ▼                     commitment 📌               auto-removed) ✅
   Any movable event
   (status, payment,
    late, rent)
```

### 🔄 The promise lifecycle

1. **📍 Detected** — any movable event dragged to a future date becomes a commitment automatically.
2. **🔄 Updated every run** — the auto-generated description section rebuilds with live balance data, while any PM notes above the divider are **always preserved**:
   - On or after today → 🔴 (full month owed) or 🟡 (partial)
   - Promised date passed, still unpaid → ⚠️ **Overdue**
   - **No auto-expire** — drag an overdue ⚠️ to a new date and it instantly becomes 🔴 / 🟡 again
3. **✅ Resolved** — balance reaches ≤ 0 and every promise for that unit is automatically removed.
4. **🛡️ Safe-delete protection** — deleting the *last* promise on a unit that still owes? The sync treats it as an accident and recreates it. A tracked unit always keeps at least one promise until it's paid in full.

### 📋 Split payment plans — installments made easy

Need to track an installment arrangement? **Copy-paste** a commitment event onto each promised date. Every copy is discovered and tracked automatically. Edit the `PROMISED:` line in each copy to note the installment amount.

### 📝 PM notes that survive every sync

Every commitment has an editable section *above* an `AUTO-SYNCED — do not edit below` divider. Type notes like `PROMISED: $500 — spoke with tenant` and they'll survive every future sync run, forever.

### Commitment source types

| Source | Came from | Behavior |
| ------ | --------- | -------- |
| `status` | Monthly status event | Represents that origin month's rent |
| `payment` | Additional payment event | Same as above |
| `late` | Today marker | Arrears — may pre-load next month's rent |
| `kickstart` | Future-month placeholder | Drag back to the 1st to revert to a placeholder |

---

## 🔍 The "today" dashboard

Open **any owner's calendar on today's date** and you get a live, zero-effort collections dashboard — every unit that owes money and isn't already tracked by a promise, all in one place.

```
   📅 Today's view
   ─────────────────────────────────────────────
   🔴 123 Main St Unit 2   — $1,200 due (Day 3 late)
   🟡 456 Oak Ave Unit 1   — $600 partial, $600 still owed
   🔴 789 Elm St Unit 4    — $950 due
   ─────────────────────────────────────────────
   (units with active promises don't appear here)
```

- **Instant insight** — no spreadsheet, no report, no login to AppFolio. Just open today.
- **Promised units are excluded** — if a unit already has a future-dated promise, it won't clutter the dashboard.
- **Broken promises resurface** — a unit whose promises have all passed appears in today's dashboard *and* shows ⚠️ overdue markers on the missed dates.
- **Grace-period aware** — markers appear from the 1st of the month onward; the `(Day N late)` tag appears only after the grace period.

---

## 🚀 Setup

### Prerequisites

- ☁️ A Google Cloud **service account** with the Calendar API enabled and a JSON key
- 🏢 An **AppFolio Plus** account with v2 Reports API credentials (client ID + secret)
- 📧 A **Google account for the PM** (`PM_EMAIL`) that will own the calendars in the UI
- 🐙 A GitHub repo with Actions enabled

### 🔑 Repository secrets

*Settings → Secrets and variables → Actions → Secrets*

| Secret | What it is |
| ------ | ---------- |
| `APPFOLIO_DB_NAME` | AppFolio database subdomain (e.g. `openkey`) |
| `APPFOLIO_CLIENT_ID` | AppFolio v2 API client ID |
| `APPFOLIO_CLIENT_SECRET` | AppFolio v2 API client secret |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full service-account JSON (paste the whole thing) |
| `PM_EMAIL` | The PM's Google account email |

### ⚙️ Repository variables

*Settings → Secrets and variables → Actions → Variables* (all optional — defaults shown in [Configuration reference](#configuration-reference)).

### 💻 Local runs

For local runs, you can keep the same values in a JSON file instead of exporting env vars each time.

1. Copy `local_config.example.json` to `local_config.json`, or create `secrets/local_config.json`.
2. Fill in the values you need for the script you are running.
3. Run the script normally.

The scripts check `secrets/local_config.json` first, then `local_config.json`, then environment variables. `secrets/` is already ignored by git, and `local_config.json` is ignored too.

### 🎯 First-run checklist

1. ▶️ Let the workflow run once — it creates all the calendars automatically.
2. 📬 Run `share_portfolio_calendars.py` once to receive "Add this calendar" emails, then click each to add them to the PM's sidebar.
3. 🔗 Share each calendar's subscription link with the corresponding owner (`list_calendars.py` prints these), or let the sync auto-share when owner emails are in AppFolio.

---

## ⚙️ Configuration reference

Production configuration still uses environment variables in `.github/workflows/sync.yml`, but local runs can use `local_config.json` or `secrets/local_config.json`.

**🔒 Required (secrets):**

| Variable | Description |
| -------- | ----------- |
| `APPFOLIO_DB_NAME` | AppFolio database subdomain |
| `APPFOLIO_CLIENT_ID` | AppFolio v2 API client ID |
| `APPFOLIO_CLIENT_SECRET` | AppFolio v2 API client secret |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service-account JSON (string or file path) |
| `PM_EMAIL` | PM Google account email |

**🔧 Optional (variables), with defaults:**

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `LATE_GRACE_DAYS` | `5` | Days after due date before a unit is flagged "late" |
| `DEFAULT_LEASE_MONTHS` | `12` | Assumed lease length when AppFolio has no end date |
| `COMMITMENT_LOOKAHEAD_MONTHS` | `3` | How many future months to scan for dragged placeholders |
| `TIMEZONE` | `America/Chicago` | Timezone used to compute "today" |
| `FORCE_REFRESH` | `false` | Rebuild all future-month events from scratch |
| `RENT_DUE_DAY` | `1` | Day of month rent is due |

---

## ⏱️ Scheduling

```
  🗓️ Normal months      ──► every 30 minutes
  🔥 Rent week (days 1–5) ──► every 15 minutes
  🖱️ Manual trigger     ──► "Run workflow" button in GitHub UI
```

After each run, `state.json` is committed back to `main` automatically. Concurrent-run safety is handled via `git pull --rebase`, and `[skip ci]` in the commit message prevents infinite loops.

---

## 💾 State file

`state.json` is the only persistence layer — committed to the repo after every run, no database required.

**Per occupancy + month** (keyed `"{occupancy_id}@{owner_id}_{YYYY-MM}"`):
- `status`, `past_due`
- `calendar_id`, `status_event_id`, `status_event_date`
- `late_event_id`, `payment_event_ids`
- Future months also store `rent_event_id` and `is_commitment`

**Commitment registry** (`state["_commitments"]["{occupancy_id}@{owner_id}"]`):
One entry per promise (multiple for split plans), each with `event_id`, `anchor_date`, `source_type`, `origin_month`, `calendar_id`, and `covers_rent_month`.

> 🏘️ **Owner-scoped keys** — state keys use `occupancy_id@owner_id`, not the bare occupancy ID. Co-owned units are processed independently per owner, with completely separate event IDs on each owner's calendar.

---

## 🛠️ Helper scripts

One-time and occasional maintenance tools — run locally with the same service-account JSON. On Windows/Miniconda, set env vars with `set` and run with `python <script>.py`.

| Script | What it does |
| ------ | ------------ |
| `list_calendars.py` | 📋 Lists every Portfolio calendar with its ID, sharing info, and subscription link |
| `grant_pm_access.py` | 🔑 One-time: grants the PM access to all portfolio calendars |
| `restrict_access.py` | 🔒 Revokes access from everyone except the PM and one specified owner (`KEEP_EMAIL`) |
| `share_portfolio_calendars.py` | 📬 Re-shares each calendar with notifications **on**, triggering fresh "Add this calendar" emails |
| `cleanup_duplicates.py` | 🧹 Scans calendars and deletes duplicate events, keeping IDs recorded in `state.json` |

> ⚠️ **Heads-up — `cleanup_duplicates.py`** uses a legacy `OKPM` name filter. After the rename to `… Portfolio`, update its filter to `summary.endswith("Portfolio")` before use. Similarly, `grant_pm_access.py` grants **reader** access while the sync now grants the PM **owner** — harmless, as the sync upgrades the role on its next run.

---

## 🔬 AppFolio API notes

Key details for anyone extending the data layer:

- **v2 Reports API only** — every request is a `POST` to `/api/v2/reports/{report}.json` (Plus accounts don't have the v1 Stack API)
- **Credentials in the URL:** `https://{CLIENT_ID}:{CLIENT_SECRET}@{DB}.appfolio.com/api/v2/reports/{report}.json`
- **Pagination** uses `next_page_url` in the response body
- **`past_due` is the source of truth** for balances — `credit_debit_balance` is always null; negative `past_due` = credit = 🩷 Prepaid
- **`tenant_ledger` ignores filters** — `occupancy_id` is silently ignored (returns all tenants); date window is unreliable — payments are filtered client-side
- **NSF events** produce two ledger rows; non-positive credits are skipped, and NSF is also detected by description keywords
- **Co-ownership** via `properties_owned_i_ds` (unusual spelling) — comma-separated property IDs joining to `rent_roll.property_id`
- **Phone numbers** arrive with a label prefix (`Phone: …`) — stripped automatically
- **Unit field is inconsistent** — normalized to always read `Unit X` (never `Unit Unit`)
- **Only `status == "Current"` leases are synced** — others are counted and logged
- **Tenant names** arrive as `Last, First` — normalized to `First Last`

---

## 🔧 Operations & troubleshooting

| Issue | Solution |
| ----- | -------- |
| 🔄 Need to rebuild all future events | Set `FORCE_REFRESH=true`, run once, set back to `false` |
| 🐢 Promises take time to settle | Normal — titles/colors refresh on cycle 1, stale events sweep on cycle 2. No `FORCE_REFRESH` needed |
| 📋 Duplicate events on the same day | Sync auto-dedupes on each run; for migration artifacts run `cleanup_duplicates.py` (fix name filter first) |
| 📆 Calendar missing from PM sidebar | Run `share_portfolio_calendars.py` and click the "Add this calendar" links |
| 🚦 Rate limits | Google Calendar retries with exponential backoff on 403/429/500/503; AppFolio waits 60s on 429 |
| ❌ One unit fails to sync | Logged with stack trace — all other units continue unaffected |

---

## 🗂️ Project structure

```
pm-calendar-sync/
├── 🔁 sync.py                        — the sync engine (all core logic)
├── 📦 requirements.txt               — requests, google-auth, google-api-python-client
├── 💾 state.json                     — persisted state (auto-committed each run)
├── ⚙️  .github/workflows/sync.yml    — GitHub Actions schedule + job
├── 📋 list_calendars.py              — helper: list calendars + links
├── 🔑 grant_pm_access.py             — helper: grant PM access (one-time)
├── 🔒 restrict_access.py             — helper: lock a calendar to PM + one owner
├── 📬 share_portfolio_calendars.py   — helper: force "Add this calendar" emails
└── 🧹 cleanup_duplicates.py          — helper: remove duplicate events
```

**Inside `sync.py`:**

```
AppFolioClient          ──► the four AppFolio report calls
data helpers            ──► name normalization, payment parsing, NSF detection
GoogleCalendarManager   ──► calendar create/share, event builders, upsert/delete
StateManager            ──► state.json reads/writes + commitment registry
SyncOrchestrator        ──► run loop, per-unit sync, commitment lifecycle, today dashboard
```
