# OKPM Sync — Calendar sidebar add-on

Two buttons in the Google Calendar **desktop web** sidebar that trigger the
sync's manual GitHub Actions modes:

| Button | Workflow mode | What it does | Typical time |
| ------ | ------------- | ------------ | ------------ |
| **Submit (~1 min)** | `submit` | Consolidates promises you dragged on the calendars (no AppFolio call; balances from the cached snapshot, ≤1h stale) | 30–60 s |
| **Update (~2 mins)** | `update` | Pulls fresh AppFolio balances/payments and re-syncs only the units whose money changed | 1.5–2 min |

The buttons are a **UX layer only** — every run (manual or scheduled) is
serialized by the workflow's concurrency group, so double clicks or a hung
card can never corrupt state; repeated clicks coalesce into one queued run,
and the hourly full sweep corrects anything missed.

Workspace add-on callbacks must return within ~30 s, so a press dispatches,
polls for up to ~22 s, and — if the run is still going — shows a status card
with **only** a **Refresh status** button; Submit/Update are hidden until the
run finishes, so a run can't be started on top of one already going (this
holds even if the panel is closed and reopened). While a press is processing
the card is also blocked. When the run completes the buttons return.

Mobile Calendar clients don't support Workspace add-ons — desktop web only.

## One-time setup (~10 minutes)

### 1. Create a GitHub token

1. GitHub → Settings → Developer settings → **Fine-grained personal access
   tokens** → Generate new token.
2. Resource owner: the account that owns `pm-calendar-sync`.
   Repository access: **Only select repositories** → `pm-calendar-sync`.
3. Repository permissions: **Actions → Read and write** (Metadata: read is
   added automatically). Nothing else.
4. Pick an expiry you can live with (you'll repeat step 3 below when it
   expires) and copy the token (`github_pat_…`).

### 2. Create the Apps Script project

1. Go to [script.new](https://script.new) (same Google account you use for
   Calendar). Name the project (e.g. `OKPM Sync add-on`).
2. Project Settings (gear icon) → check **“Show ‘appsscript.json’ manifest
   file in editor”**.
3. In the editor, replace the contents of `appsscript.json` with this
   folder's [`appsscript.json`](appsscript.json), and replace `Code.gs` with
   this folder's [`Code.gs`](Code.gs). Save.

### 3. Set Script Properties

Project Settings → **Script Properties** → add:

| Property | Value |
| -------- | ----- |
| `GITHUB_PAT` | the token from step 1 |
| `GH_OWNER` | `caraido` |
| `GH_REPO` | `pm-calendar-sync` |
| `GH_WORKFLOW` | *(optional)* `sync.yml` |
| `GH_REF` | *(optional)* `main` |

When the PAT expires, generate a new one and update `GITHUB_PAT` here —
nothing else changes.

### 4. Install the test deployment

1. **Deploy → Test deployments → Install** (application: Google Workspace
   Add-on), then **Done**.
2. Open [calendar.google.com](https://calendar.google.com) on desktop and
   look for the add-on's sync icon in the **right-hand side panel** (reload
   the tab if it doesn't appear).
3. First click asks for authorization: the add-on needs only “connect to an
   external service” (the GitHub API) and “run as a Calendar add-on”.

## Troubleshooting

- **“Set Script Properties …” on the card** — step 3 wasn't completed.
- **`Dispatch failed: HTTP 401`** — PAT invalid or expired; regenerate.
- **`Dispatch failed: HTTP 404`** — PAT can't see the repo (wrong resource
  owner / repository access) or `GH_OWNER`/`GH_REPO`/`GH_WORKFLOW` is wrong.
- **`Dispatch failed: HTTP 422`** — the workflow on `main` doesn't have the
  `mode` dispatch input (the repo's Phase C workflow changes aren't pushed).
- **“Queued — run not visible yet”** — GitHub hadn't materialized the run
  within the poll budget; press **Refresh status**. If two people dispatch
  within ~15 s the card can attach to the other person's run — harmless
  (both runs are serialized; check the Actions tab if in doubt).
- Execution logs: Apps Script editor → **Executions** (left rail).
