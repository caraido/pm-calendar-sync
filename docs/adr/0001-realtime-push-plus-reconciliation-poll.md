# ADR-0001: Real-time calendar sync via Push + Reconciliation Poll

**Status:** Proposed
**Date:** 2026-06-13
**Deciders:** Alec (maintainer)
**Supersedes:** the GitHub Actions polling model (15/30-min cron)

---

## Context

Today the sync runs as a GitHub Actions cron (every 30 min, every 15 min on days 1–5). Each run pulls four AppFolio v2 reports, writes per-owner Google Calendars, and commits `state.json` back to the repo. It is free, stateless, and effectively zero-maintenance, but a PM action — dragging a promise event — isn't registered until the next tick (up to ~30 min later).

There are two independent data-flow directions:

- **AppFolio → calendar** (payments, leases change upstream). AppFolio Plus exposes only the **pull-based v2 Reports API**; its signed (JWS) webhooks belong to the **Stack** partner program, which a Plus/Reports-only account most likely cannot use. *Confirm with the AppFolio rep before assuming webhooks are available.*
- **Calendar → state** (PM drags a promise). Google Calendar supports **push** notifications (`events.watch`), so this direction can be made real-time today, independent of AppFolio.

This ADR audits the **Phase 1 hybrid**: Google push for instant drag handling, plus a low-frequency **reconciliation poll** for correctness and for the still-polled AppFolio direction, all on a single always-on service, retiring GitHub Actions.

### Constraints

- Solo maintainer, minimal budget, no existing server infrastructure.
- The data is sensitive: tenant names, balances, addresses, phone numbers (PII + financial).
- The service account currently **owns every owner calendar** — wide blast radius.

---

## Decision

Stand up one small always-on service that:

1. Receives Google Calendar push notifications and handles drags within seconds (lock → incremental `syncToken` delta → existing commitment logic → write back).
2. Runs a **reconciliation poll** (full sweep, both directions) on a relaxed interval as a self-healing safety net and as the AppFolio data path.
3. Persists state in **SQLite** on the host (replacing git-committed `state.json`).

GitHub Actions is retired. AppFolio stays polled; Stack webhooks remain a future, optional upgrade that would let the reconciliation interval relax further.

> **Key point:** retiring Actions and fixing drag latency do **not** depend on AppFolio webhooks. They depend only on (a) somewhere to run the poll and (b) Google push.

---

## Options Considered

### Option A: Status quo, smarter polling (stay on Actions)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low |
| Cost ($) | Free |
| Ops / on-call | ~None |
| Security surface | Minimal (no inbound endpoint) |
| Drag latency | Minutes (poll interval) — unchanged |

**Pros:** No new infra, no PII custody change, no on-call. Differential cadence (poll the volatile `tenant_ledger` often, heavy directories rarely) is a cheap improvement.
**Cons:** Never real-time; bounded by Actions minutes and AppFolio rate limits.

### Option B: Pure push (no poll)
| Dimension | Assessment |
|-----------|------------|
| Complexity | High |
| Cost ($) | Low |
| Ops / on-call | High |
| Security surface | Public endpoint + secrets/PII at rest |
| Drag latency | Seconds |

**Pros:** Real-time, event-proportional work.
**Cons:** **Not self-healing** — any downtime silently drops notifications. Unacceptable on its own for financial data.

### Option C: Push + reconciliation poll *(recommended)*
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium–High |
| Cost ($) | Low (~$60–85/yr) |
| Ops / on-call | Medium |
| Security surface | Public endpoint + secrets/PII at rest |
| Drag latency | Seconds, with a poll-interval correctness floor |

**Pros:** Seconds-latency on drags; the poll caps every push failure mode at "as good as today"; clean path to full event-driven later.
**Cons:** You become a service operator and a custodian of PII on internet-exposed infra (see audit).

### Option D: Fully event-driven (AppFolio Stack webhooks + Google push) — *future*
Gated on AppFolio granting Stack access. Cheapest at scale and real-time both directions; revisit if/when available.

---

## Audit

### 1. Security risk

Moving from an ephemeral CI runner to an always-on, internet-exposed service is the largest change in the security posture. New surface, ranked by concern:

**a. Secrets at rest (highest regression).** Today AppFolio credentials and the Google service-account JSON live in GitHub's managed encrypted secrets. On a self-run host they sit on a disk you own, long-lived. Mitigate with a secret manager (cloud platform secrets, or at minimum root-only file perms + full-disk encryption), never in git, and rotate on a schedule. If you use a managed platform, prefer its secret store over plaintext env vars.

**b. Service-account blast radius.** The SA owns *all* owner calendars and can pull *all* AppFolio financial data. A compromise of this host exposes every owner's calendar and tenant PII. The blast radius equals today's, but it now lives on a persistent, reachable target rather than a throwaway runner. Consider least-privilege (does the SA need owner, or would writer + a one-time ACL setup do?) and isolating credentials.

**c. Public inbound endpoint.** Google push is the entry point an attacker can also reach. Note Google's notifications are **not cryptographically signed** the way AppFolio's JWS webhooks are — your defense is the **`X-Goog-Channel-Token`** (a secret you set at `watch` time) plus matching the channel/resource IDs against your registry. Treat that token as a secret, rotate it on renewal, reject anything that fails the check, and have the endpoint do nothing but authenticate + enqueue (the payload is thin and untrusted). Put it behind a platform edge/Cloudflare if possible.

**d. Denial-of-service / amplification.** Because a notification triggers Google API work, a flood of forged pings could amplify into quota exhaustion. Rate-limit, dedupe by `X-Goog-Message-Number`, cap the queue, and return fast.

**e. PII / data at rest.** SQLite will hold tenant names, balances, addresses, and phones. That is sensitive landlord/tenant data: encrypt the disk and backups, restrict access, set a retention policy, and keep PII out of logs. Even for a small operation this is worth a deliberate policy.

**f. TLS & supply chain.** A valid cert is mandatory (Let's Encrypt auto-renew); an expired cert silently stops delivery (caught only by reconciliation). A long-lived web stack also needs a patch cadence that the pinned, ephemeral Actions runner never required.

### 2. Management / time cost

This is the cost that dominates — not dollars, attention.

**One-time setup (~3–6 focused days, more with hardening):** provision host, domain, DNS, TLS, Google domain verification; build the webhook receiver, worker (lock/debounce/idempotency), `events.watch` registration + renewal, and the echo-filter; migrate `state.json` → SQLite; test against simulated drags, expired tokens, and echo events.

**Ongoing (low but non-zero, and recurring):**
- OS/dependency patching: ~1–2 hrs/month.
- Channel-renewal automation: set-and-forget once built, but failures need attention.
- Cert renewal & SQLite backups: automate, then periodically verify restores.
- **Monitoring/alerting is not optional.** Without a heartbeat + alert on "service down," a weekend outage means drags silently miss until you notice (reconciliation limits the damage but latency reverts to the poll). Building and maintaining this is the hidden recurring cost.
- **On-call.** When a channel storm, rate-limit lockout, or echo loop happens, you are the responder. Actions had effectively zero on-call.

**Cognitive load.** A distributed, event-driven system with concurrency is materially harder to reason about and debug than one linear cron log. For a solo maintainer running something landlords depend on, the "is it up?" tax is real.

*Baseline for comparison:* Actions ≈ 0 ops hours, 0 on-call, 0 servers.

### 3. Reliability / correctness

- **Reconciliation poll is mandatory, not optional** — it makes the worst case (push fully broken) no worse than today, and repairs anything push dropped.
- **New bug classes:** concurrency (needs per-calendar/unit locking), duplicate/out-of-order notifications (handlers must be idempotent), `syncToken` invalidation → `410 Gone` (needs full-resync fallback).
- **Echo loop is the highest-severity functional risk.** Your own writes (locked-event snap-back, commitment auto-section rebuild) generate notifications; without a content-hash/marker filter you get a write→notify→write loop that can exhaust API quota and stall the whole system. Must be filtered and tested.
- The existing design is largely **convergent** (state derived from live truth), which is exactly what makes idempotent, unordered processing tractable — a real asset here.

### 4. Cost ($)

- Host: ~$5–7/mo VPS, or serverless scale-to-zero (~pennies, but cold-start latency + more infra moving parts).
- Google Calendar API: comfortably within free quota at this scale; an echo loop is the only realistic way to blow it (an operational risk, not a normal cost).
- AppFolio: unchanged (still polled).
- Net new ≈ **$60–85/yr** plus your time. Time, not money, is the real price.

### 5. Migration / rollback

- **Migrate:** build alongside the existing setup; one-time `state.json` → SQLite script (keep the JSON as a backup). Cut over by disabling Actions and enabling the service pointed at SQLite. Avoid running both writers against different stores simultaneously (split-brain).
- **Rollback is low-risk** thanks to convergence: re-enable the Actions workflow (keep the file, just disabled) and, worst case, run a `FORCE_REFRESH` full rebuild. No irreversible data step.

### 6. Other aspects

- **Bus factor.** A running service raises single-maintainer risk — if you're unavailable, who restarts it? Actions needed no babysitting. A managed platform with auto-restart mitigates this.
- **Vendor dependence.** You rely on Google push semantics (channel TTL, thin notifications) — stable but Google-controlled; AppFolio's Reports API remains the more fragile dependency regardless of architecture.
- **Observability.** Define "healthy" (last successful reconcile, channel freshness, queue depth) and alert on it; you lose the implicit "did the Action pass?" signal.
- **Audit trail.** Git-committed `state.json` gave a free versioned history; SQLite gives that up unless you add change logging/backups.
- **Scale.** Fine for tens–hundreds of owners; channels and renewal scale linearly. Not a concern at this size.

---

## Trade-off Analysis

The hybrid buys **seconds-latency on the one user-visible action** (drags) at the price of **becoming a service operator and a custodian of tenant PII on internet-exposed infrastructure.** The reconciliation poll is the linchpin: it caps every push failure mode at today's behavior, turning "real-time" into a strict improvement rather than a riskier trade. The dominant new costs are **operational attention and security surface**, not money (~$70/yr). The decision therefore hinges on a single judgment: *is instant drag feedback valuable enough to the PM to justify a small but real, ongoing ops/on-call/security burden?* If drag latency is merely annoying rather than costly, **Option A (smarter polling)** delivers most of the perceived freshness with none of the new surface.

---

## Recommendation

Proceed with **Option C only if instant drag feedback is genuinely valued**. If proceeding, reduce the audited costs deliberately:

- Prefer a **managed platform** (Cloud Run / Fly / Render) over a raw VPS — it offloads TLS, patching, restarts, and uptime, directly cutting the ops/bus-factor cost.
- Use the platform's **secret manager**, not plaintext env vars.
- Treat **uptime alerting** and the **reconciliation poll** as day-one requirements, not add-ons.
- Build the **echo-filter with tests** before pointing channels at production.

If instant drags aren't worth it, adopt **Option A** (differential-cadence polling) and stop there.

---

## Consequences

- **Easier:** instant drag registration; cheaper, event-proportional reaction; a foundation for full event-driven sync if AppFolio webhooks arrive.
- **Harder:** operations, security posture, debugging, on-call, and PII custody.
- **Revisit when:** AppFolio Stack webhooks become available (relax/remove reconciliation), or maintainer bandwidth changes (reconsider managed vs. self-hosted, or reverting to Option A).

---

## Open Decisions (need your input)

1. **Host:** managed platform (recommended) vs. raw VPS.
2. **Secret storage:** platform secret manager vs. encrypted env/file.
3. **Reconciliation interval:** e.g. 15 min, tightened during rent week.
4. **Alerting channel:** email / SMS / Slack for "service down."
5. **PII policy:** disk/backup encryption and retention.

## Action Items

1. [ ] Confirm with AppFolio rep whether Stack webhooks are available (decides if Option D is ever reachable).
2. [ ] Decide host + secret storage (Open Decisions 1–2).
3. [ ] Spike `events.watch` registration + renewal against one calendar.
4. [ ] Implement SQLite `StateManager` (match current interface) + migration script.
5. [ ] Implement webhook receiver + worker with locking, debounce, idempotency, and echo-filter (+ tests).
6. [ ] Add reconciliation poll (reuse existing orchestrator sweep) + uptime alerting.
7. [ ] Cut over: disable Actions, enable service; keep workflow file for rollback.
