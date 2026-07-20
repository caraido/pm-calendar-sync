# 📅 OKPM Calendar Sync

> **Your entire rental portfolio, live in Google Calendar — zero servers, zero spreadsheets, zero chasing.**

OKPM Calendar Sync automatically pulls real-time rent and payment data from AppFolio and turns it into beautiful, color-coded Google Calendars — **one per AppFolio property group**. The PM gets a full interactive collections dashboard, organized exactly the way the portfolio is grouped in AppFolio. Everyone always knows exactly who owes what, right now.

> 🔀 **July 2026 — group cutover.** Calendars used to be one-per-owner; they're now one-per-property-group, and the old owner calendars were retired in place (renamed `[RETIRED] …`, history preserved, PM-only access). Edit the groups in AppFolio and the calendars follow automatically.

**Runs free on GitHub Actions. No server to maintain. No database to manage. Just plug in your credentials and go.**

---

## ✨ Why you'll love it

| | Feature | What it means for you |
|---|---|---|
| 🔄 | **Auto-syncs every hour** | Payment data is always fresh — every 30 min during rent week (days 1–5) |
| 📆 | **One calendar per property group** | Calendars mirror your AppFolio property groups — rename or reorganize groups in AppFolio and the calendars follow |
| 🎨 | **Instant visual status** | Color-coded events tell the whole story at a glance |
| 👆 | **Drag to track promises** | Log a promise-to-pay in seconds — just drag an event to the date |
| 🤝 | **Split payment plans** | Copy-paste a commitment to map out installment arrangements |
| 🔍 | **Read a month at a glance** | Color-coded status events + promise markers show who owes what — no spreadsheet |
| 🏘️ | **Multi-group support** | A property in several groups appears on every one of those calendars, promises kept in sync across them |
| 🔐 | **PM-only by design** | Groups can span unrelated owners, so calendars are shared with the PM account only |
| 💸 | **$0 infrastructure cost** | Runs entirely on free GitHub Actions |

---

## 📋 Table of contents

* [How it works](#how-it-works)
* [The calendars](#the-calendars)
* [The visual language](#the-visual-language-status-model)
* [Event types](#event-types)
* [The monthly lifecycle](#the-monthly-lifecycle)
* [Promise-to-pay](#promise-to-pay-commitments)
* [Reading the calendar at a glance](#reading-the-calendar-at-a-glance)
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
│  (v2 Reports API)   │──────► │  (GitHub Actions)    │──────► │  (one cal / group)  │
│                     │        │                      │        │                     │
│  📋 rent_roll       │        │  ✅ builds status,   │        │  🖱️ PM owns + drags  │
│  🗂️ property_groups │        │  💳 payment,         │        │     events to manage│
│  🧑 tenant_dir      │        │  📌 placeholder &    │        │     (PM-only)       │
│  📒 tenant_ledger   │        │     commitment events│        │                     │
└─────────────────────┘        └──────────────────────┘        └─────────────────────┘
```

Each run, the sync engine:

1. 📥 **Pulls four AppFolio reports** — `rent_roll`, `property_group_directory`, `tenant_directory`, and the current month's `tenant_ledger`.
2. 🔗 **Maps every active lease to its property group(s)** — a property in several groups appears on each of those calendars simultaneously; properties in no group are intentionally unsynced.
3. 📆 **Finds or creates each group's calendar** — summary is the AppFolio group name verbatim; the nightly verifies each calendar **by id** and renames it if the group was renamed in AppFolio.
4. ✏️ **Writes / updates / removes calendar events** — reflecting each unit's live payment situation.
5. 💾 **Saves `state.json`** and commits it back to the repo automatically.

No server. No database. The only moving parts are the GitHub Actions workflow, the Python script, and the committed state file.

---

## 📆 The calendars

Each AppFolio **property group** gets one dedicated calendar named after the group **verbatim** (e.g., `L&P Midwest Capital`, `Tian Xin Property Group`) — everything neatly separated, nothing mixed together. Rename a group in AppFolio and the nightly run renames the calendar in place (same ID, same events).

```
┌────────────────────────────────────────────────────┐
│  👑 Service account  ──► owns all calendars        │
│  🏢 PM account       ──► full owner access         │
│                           (drag, edit, manage)      │
│  🚫 Nobody else      ──► groups can span unrelated │
│                           owners, so calendars are  │
│                           PM-only by design         │
└────────────────────────────────────────────────────┘
```

> 🔀 **The retired owner calendars (July 2026 cutover):** the previous one-calendar-per-owner setup was retired in place — each old calendar renamed `[RETIRED] {Owner} Portfolio`, non-PM access revoked, events frozen as history. Pre-cutover payment history lives there; the group calendars build forward from the cutover. Active promise-to-pay events were migrated automatically (PM notes preserved). To undo the cutover, see `misc/rollback_group_cutover.py`.

> 📬 **Sidebar tip:** Sharing grants *access*, but Google doesn't auto-add an API-shared calendar to the sidebar. Log in as the PM and add each calendar once — `python tests/list_calendars.py` prints every group calendar's ID and a clickable subscription link.

---

## 🎨 The visual language (status model)

No more digging through spreadsheets. Every event has a leading emoji and a matching Google Calendar color — the PM can read an entire month in seconds.

```
   🩷 Prepaid  ──────────────────────── flamingo pink  (credit balance)
   ✅ Paid     ──────────────────────── sage green     (balance = $0)
   🟡 Partial  ──────────────────────── banana yellow  (partial payment)
   🔴 Unpaid   ──────────────────────── tomato red     (full month owed)
```

| Status | Condition | Color |
| ------ | --------- | ----- |
| 🩷 **Prepaid** | Credit / overpaid (`past_due < 0`) | Flamingo pink |
| ✅ **Paid** | Fully paid (`past_due == 0`) | Sage green |
| 🟡 **Partial** | Partial payment (`0 < past_due < rent`) | Banana yellow |
| 🔴 **Unpaid** | Full month owed (`past_due >= rent`) | Tomato red |
| ⚪ **Settled** | An *earlier* / NSF payment, once the month is fully paid | Graphite grey |

The live `past_due` balance from AppFolio is the single source of truth — including negative values (credits), which trigger the 🩷 Prepaid state automatically.

> ⚪ **Once a unit is paid in full, its earlier payment events fade to grey.** When the month's balance reaches zero (or a credit), every *previous* payment event that was 🟡 Partial or 🔴 NSF is recolored graphite grey — the visual noise disappears so the PM's eye goes straight to the units that still owe. The payment that actually **settled** the month stays ✅ green (so you can see exactly when they paid up), and any 🩷 prepaid/credit event stays pink. Nothing greys while a balance remains.

> 📌 **Commitments use the same colors.** A promise-to-pay event takes the color of its current balance — 🔴 when nothing has been paid this month, 🟡 when a partial payment leaves a balance. A promise whose date has already passed is **not** specially flagged; it simply keeps its 🔴 / 🟡 color until it's renegotiated or paid. *(There is no separate "overdue" color — the old ⚠️ tangerine state was removed.)*

---

## 📌 Event types

Four active event types work together to tell the complete story of every unit's payment history and future:

| Type | 📍 Appears on | Purpose |
| ---- | ------------- | ------- |
| `status` | 1st of month → migrates to first payment date | The month's headline status for a unit |
| `payment` | Each payment date after the first | Logs every additional payment individually |
| `rent` | 1st of future months | Frozen placeholder for upcoming rent |
| `commitment` | A promised payment date | Tracks a promise-to-pay the PM is managing |
| ~~`late`~~ | *(retired)* | Former "today" marker — **no longer created**; see [below](#reading-the-calendar-at-a-glance) |

> ⚠️ **`late` is retired.** Earlier versions placed a "today" marker event on the current date as a collections dashboard. That event type is gone: each run now **deletes** any leftover `late` event it finds. Promise-to-pay commitments that were originally created by dragging an old `late` marker (`source_type = "late"`) are still tracked and managed normally — they're just never created anymore.

### 🖱️ Drag to manage — movable vs. locked

The PM's primary interaction is **dragging events** on the calendar:

- **Movable** — drag any of these to a future date to instantly create a promise-to-pay:
  - Future-month placeholders (`rent`) → `kickstart` commitment
  - Status events (`status`)
  - Payment events (`payment`)
- **Locked** — status and payment events that haven't been dragged into a promise snap back to their correct date on the next sync, protecting against accidental moves.

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
2. **🔄 Updated every run** — the auto-generated description section rebuilds with live balance data, while any PM notes above the divider are **always preserved**. The color tracks the live balance:
   - 🔴 **Unpaid** — nothing paid this month (full month owed)
   - 🟡 **Partial** — a partial payment leaves a balance
   - **No special "overdue" state** — a promise whose date has already passed keeps its 🔴 / 🟡 color (no auto-expire); it stays put until renegotiated or paid.
3. **✅ Resolved** — balance reaches ≤ 0 and every promise for that unit is automatically removed.
4. **🛡️ Safe-delete protection** — deleting the *last* promise on a unit that still owes? The sync treats it as an accident and recreates it. A tracked unit always keeps at least one promise until it's paid in full.

### 📋 Split payment plans — installments made easy

Need to track an installment arrangement? **Copy-paste** a commitment event onto each promised date. Every copy is discovered and tracked automatically. Edit the `PROMISED:` line in each copy to note the installment amount.

### 🗓️ Promise into next month — combined total, itemized

Tenant says they'll cover *this* month's rent *next* month? Drag the event into next month and the promise event shows the **combined amount they'll owe by then**, broken down:

```
🔴 · Jane Doe · Unit 2 · Main St · $2,800 owed · Promise Aug 10, 2026
────────────────────────────────────────────
Outstanding:  $2,800.00  (combined)
   • Previous balance: $400.00
   • This month (Jul 2026): $1,200.00
   • Promised (Aug 2026): $1,200.00
```

The promise **absorbs next month's placeholder** on the 1st (no duplicate event), folding that month's rent into the combined total. Every figure is recomputed **live from AppFolio's current balance each run** — there's no stored per-tenant history to drift or corrupt when a sync run fails, so the numbers are always right on the next successful run. *("Previous balance" is the lump sum older than this month; the total is always exact.)* If the tenant pays or you drag the promise back, the placeholder reappears automatically.

### 📝 PM notes that survive every sync

Every commitment has an editable section *above* an `AUTO-SYNCED — do not edit below` divider. Type notes like `PROMISED: $500 — spoke with tenant` and they'll survive every future sync run, forever.

### Commitment source types

| Source | Came from | Behavior |
| ------ | --------- | -------- |
| `status` | Monthly status event | Represents that origin month's rent |
| `payment` | Additional payment event | Same as above |
| `kickstart` | Future-month placeholder | Drag back to the 1st to revert to a placeholder |
| `late` *(legacy)* | Old "today" marker | Arrears; may pre-load next month's rent. **No longer created** — only pre-existing ones are still managed |

---

## 🔍 Reading the calendar at a glance

> 🗓️ **Note — the dedicated "today" marker was retired.** Earlier versions stamped a single dashboard event on today's date listing everyone who owed money. That marker is gone; the sync now deletes any leftover ones. Collections are read directly off the events that are already there, which stay current automatically.

You don't need a spreadsheet or an AppFolio login to see who owes what. Open any group's calendar for the current month and the picture is in the colors:

```
   📅 This month's view
   ─────────────────────────────────────────────
   🔴 123 Main St Unit 2   — $1,200 due          (status event, 1st)
   🟡 456 Oak Ave Unit 1   — $600 paid, $600 left (migrated status event)
   ✅ 789 Elm St Unit 4    — paid in full
   🔴 321 Pine Rd Unit 1   — $950 owed · Promise Jun 20   (commitment)
   ─────────────────────────────────────────────
```

- **Color is the signal** — 🔴 unpaid, 🟡 partial, ✅ paid, 🩷 prepaid; the [status model](#the-visual-language-status-model) tells the whole story for each unit.
- **Promises are visible** — every promise-to-pay is its own commitment event sitting on the promised date, colored 🔴 / 🟡 by its current balance.
- **Broken promises keep their color** — once a promised date passes unpaid, the commitment stays on its date and keeps its 🔴 / 🟡 balance color until renegotiated or paid. No auto-expire, no special overdue color.
- **Grace-period aware** — each status/placeholder event records its `Late After:` date (due date + the tenant's grace days) in the description.

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

The **helper scripts** (`grant_pm_access.py`, `restrict_access.py`, `share_portfolio_calendars.py`, and the tools under `tests/` and `misc/`) read config through `local_config.py`, so you can keep credentials in a JSON file instead of exporting env vars each time.

1. Create `local_config.json` in the repo root, or `secrets/local_config.json`.
2. Fill in the keys you need (same names as the [repository secrets](#-repository-secrets)) as a JSON object.
3. Run the script normally.

`local_config.py` resolves each key by checking `secrets/local_config.json` first, then `local_config.json`, then environment variables. `secrets/` is git-ignored, and `local_config.json` is too.

> ⚠️ **The main sync is the exception.** `pm_calendar_sync/config.py` reads the required AppFolio/Google values straight from environment variables (it does **not** consult `local_config.json`). To run `python sync.py` locally, export the env vars first — see the `set …` example at the top of `smoke_test.py`.

### 🎯 First-run checklist

1. ▶️ Let the workflow run once — it creates all the group calendars automatically and shares each with the PM (owner role).
2. 📬 Log in as the PM and add each calendar to the sidebar once — `python tests/list_calendars.py` prints each group calendar's ID and subscription link (API-granted access doesn't auto-add calendars to the sidebar).
3. 🗂️ Whenever you add or reorganize property groups in AppFolio, the sync picks it up automatically — new groups get calendars, renamed groups get renamed calendars (nightly).

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
  🗓️ Days 6–31            ──► hourly full sweep
  🔥 Rent week (days 1–5)  ──► every 30 minutes
  🌙 Nightly (8:15 UTC)    ──► full_nightly: refreshes the directory cache,
                               verifies calendars by id, re-asserts PM access
  🖱️ Manual trigger        ──► "Run workflow" in GitHub UI (or the Calendar
                               add-on) with mode = submit / update / full
```

**Run modes:** the `full` sweep is the sole authority for correctness. `update` (re-sync only units whose money moved) and `submit` (consolidate PM drags into promises, no AppFolio call) are fast paths over cached data — anything they miss is corrected by the next full sweep within the hour.

After each run, `state.json` and `cache/` are committed back to `main` automatically. All runs share one concurrency group (strictly serial, never cancelled mid-flight), and `[skip ci]` in the commit message prevents infinite loops.

---

## 💾 State file

`state.json` is the only persistence layer — committed to the repo after every run, no database required.

**Per occupancy + month** (keyed `"{occupancy_id}@g{group_id}_{YYYY-MM}"`):
- `status`, `past_due`
- `calendar_id`, `status_event_id`, `status_event_date`
- `payment_event_ids`, `payment_event_dates`, `payments`
- `late_event_id` — retained for backward compatibility but now always `null` (any pre-existing `late` event is deleted on the next run)
- Future months also store `rent_event_id` and `is_commitment`

**Commitment registry** (`state["_commitments"]["{occupancy_id}@g{group_id}"]`):
One entry per promise (multiple for split plans), each with `event_id`, `anchor_date`, `source_type` (`status` / `payment` / `kickstart`, or legacy `late`), `origin_month`, `calendar_id`, and `covers_rent_month`.

**Cutover bookkeeping** (July 2026): `state["_calendars"]["g{group_id}"]` caches each group's calendar id for the fast modes; `state["_retired_calendars"]` records the retired owner calendars (input to the rollback script); `state["_migrations"]["group_cutover_v1"]` is the one-time cutover marker.

> 🏘️ **Group-scoped keys** — state keys use `occupancy_id@g{group_id}`, not the bare occupancy ID (the `g` prefix keeps them distinct from the purged legacy `@{owner_id}` keys). A property in several groups is processed independently per group, with completely separate event IDs on each group's calendar — but Google events themselves are tagged with the *bare* occupancy id, so the event model is scope-independent.

---

## 🛠️ Helper scripts

One-time and occasional maintenance tools — run locally with credentials in `local_config.json` / `secrets/local_config.json` (or env vars). On Windows/Miniconda, you can also `set` env vars and run with `python <path>.py`.

**Root-level helpers:**

| Script | What it does |
| ------ | ------------ |
| `smoke_test.py` | 🧪 Offline self-check: imports every module, verifies the package wires together, and exercises core behaviors incl. the group cutover. Hits no network (Google client is mocked) |
| `grant_pm_access.py` | ⚠️ **Legacy (pre-cutover)** — its name filter now matches only the retired owner calendars; do not run |
| `restrict_access.py` | ⚠️ **Legacy (pre-cutover)** — same filter caveat; do not run against the live setup |
| `share_portfolio_calendars.py` | ⚠️ **Legacy (pre-cutover)** — useful only when rolling the cutover back |

**`tests/` — read-only inspection tools (run from the repo root):**

| Script | What it does |
| ------ | ------------ |
| `tests/list_calendars.py` | 📋 Lists every managed group calendar (from `state.json`) with its ID, sharing info, and subscription link; retired calendars counted separately |
| `tests/inspect_appfolio.py` | 🔎 Dumps raw AppFolio report rows for debugging |
| `tests/inspect_payment.py` | 🔎 Inspects how a tenant's ledger payments are parsed |
| `tests/probe_matching.py` | 🔎 Dumps every event on a managed calendar (by group-name substring) with its okpm tags |

**`misc/` — state-repair / cleanup tools (use with care, they mutate calendars or `state.json`):**

| Script | What it does |
| ------ | ------------ |
| `misc/rollback_group_cutover.py` | ⏪ Un-retires the legacy owner calendars (strips `[RETIRED] `, optionally re-shares owners) — the rollback path for the group cutover; run **before** the next nightly |
| `misc/clean_keep_commitments.py` | 🧹 Removes events while preserving commitment (promise) events — operates on the group calendars from `state.json` |
| `misc/repair_state.py` | 🛠️ Rebuilds/repairs `state.json` from what's live on the calendars |
| `misc/fix_state_json.py` | 🛠️ Fixes encoding/structure issues in `state.json` |
| `misc/cleanup_duplicates.py` | ⚠️ **Legacy twice over** (pre-rename "OKPM" filter *and* pre-cutover) — kept for reference only |

---

## 🔬 AppFolio API notes

Key details for anyone extending the data layer:

- **v2 Reports API only** — every request is a `POST` to `/api/v2/reports/{report}.json` (Plus accounts don't have the v1 Stack API)
- **Credentials in the URL:** `https://{CLIENT_ID}:{CLIENT_SECRET}@{DB}.appfolio.com/api/v2/reports/{report}.json`
- **Pagination** uses `next_page_url` in the response body
- **`past_due` is the source of truth** for balances — `credit_debit_balance` is always null; negative `past_due` = credit = 🩷 Prepaid
- **`tenant_ledger` ignores filters** — `occupancy_id` is silently ignored (returns all tenants); date window is unreliable — payments are filtered client-side
- **NSF events** produce two ledger rows; reversals arrive as separate *negative-credit* rows (matched by reference token or amount, and reconciled onto the flipped payment event); NSF is also detected by description keywords
- **Property groups** come from `property_group_directory` — one row per property × group membership; a property can belong to several groups, and unassigned properties arrive with `property_group_id: null` (intentionally unsynced)
- **`properties_owned_i_ds`** (unusual spelling) still exists in `owner_directory` but is no longer pulled — grouping is by property group, not owner
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
| 📋 Duplicate events on the same day | Sync auto-dedupes on each run |
| 📆 Calendar missing from PM sidebar | API-shared calendars don't auto-appear — add each once via the links from `python tests/list_calendars.py` (logged in as the PM) |
| 🗂️ A property is on no calendar | Check it's assigned to a property group in AppFolio — ungrouped properties are intentionally unsynced |
| 🚦 Rate limits | Google Calendar retries with exponential backoff on 403/429/500/503; AppFolio waits 60s on 429 |
| ❌ One unit fails to sync | Logged with stack trace — all other units continue unaffected |

---

## 🗂️ Project structure

The core logic lives in the **`pm_calendar_sync/`** package. `sync.py` is now a thin entry-point shim (it just calls `SyncOrchestrator().run()`), kept so the existing GitHub Actions command — `python sync.py` — keeps working unchanged.

```
pm-calendar-sync/
├── 🔁 sync.py                         — entry-point shim → pm_calendar_sync.orchestrator
├── 🏁 __main__.py                     — `python -m` entry point
├── 📦 pm_calendar_sync/               — the sync engine (all core logic)
│   ├── __init__.py                    — package docstring (the full event/commitment model)
│   ├── config.py                      — env vars, constants, colors, logger
│   ├── appfolio.py                    — AppFolioClient: the four v2 report calls
│   ├── transforms.py                  — pure helpers: name/phone/unit normalization,
│   │                                    group mapping, payment parsing, NSF detection
│   ├── status.py                      — status classification + color / emoji mapping
│   ├── state.py                       — StateManager: state.json + commitment registry
│   │                                    + cutover bookkeeping
│   ├── cache.py                       — committed cache/ snapshots for the fast modes
│   ├── calendar_manager.py            — GoogleCalendarManager: group calendar
│   │                                    create/verify/retire, event builders,
│   │                                    find / upsert / delete
│   └── orchestrator.py                — SyncOrchestrator: run loop, per-unit sync,
│                                        future months, commitments, group cutover
├── 🧪 smoke_test.py                   — offline package self-check (no network)
├── 🧰 local_config.py                 — config loader for the helper scripts
├── 📱 addon/                          — Google Calendar sidebar add-on (Apps Script;
│                                        Submit / Update buttons, deployed manually)
├── 🔎 tests/                          — read-only inspection tools (list_calendars, etc.)
├── 🛠️  misc/                          — state-repair / rollback / cleanup tools
├── 🗄️ cache/                          — committed data snapshots (directories, rent roll)
├── 📦 requirements.txt                — requests, google-auth, google-api-python-client
├── 💾 state.json                      — persisted state (auto-committed each run)
└── ⚙️  .github/workflows/sync.yml     — GitHub Actions schedule + job
```
