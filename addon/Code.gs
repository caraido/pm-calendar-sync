/**
 * OKPM Sync — Google Calendar sidebar add-on (desktop web client).
 *
 * Two buttons that dispatch the pm-calendar-sync GitHub Actions workflow:
 *   Submit (~1 min)  — RUN_MODE=submit : consolidate dragged promises
 *   Update (~2 mins) — RUN_MODE=update : refresh changed balances/payments
 *
 * Data-integrity note: these buttons are a UX layer ONLY.  The workflow's
 * concurrency group serializes every run server-side, so a double click, a
 * hung card, or a lost poll can never corrupt state — worst case is one
 * coalesced extra run and the hourly full sweep as backstop.
 *
 * Workspace add-on callbacks must return a card within ~30 seconds, so each
 * button press dispatches, then polls for up to ~22s; if the run is still
 * going, it returns a status card with a "Refresh status" button (each press
 * polls again).  CardService blocks per press, which is what greys the
 * buttons while processing.
 *
 * Script Properties (Project Settings → Script Properties):
 *   GITHUB_PAT   fine-grained PAT for the repo, permission Actions: R/W
 *   GH_OWNER     e.g. "caraido"
 *   GH_REPO      e.g. "pm-calendar-sync"
 *   GH_WORKFLOW  optional, default "sync.yml"
 *   GH_REF       optional, default "main"
 */

var GITHUB_API       = 'https://api.github.com';
var POLL_INTERVAL_MS = 5000;
var POLL_BUDGET_MS   = 22000; // stay well under the ~30s add-on callback cap

// ── Entry points ────────────────────────────────────────────────────────────

function onHomepage(e) {
  try {
    var cfg  = cfg_();
    var last = latestManualRun_(cfg);
    if (last && last.status !== 'completed') {
      // A manual run is still going: show the status card with ONLY the
      // Refresh button, so it can't be dispatched again — even after the
      // panel is closed and reopened.
      return runningCard_('', last.id,
                          new Date(last.created_at).getTime(),
                          pretty_(last.status));
    }
    return homeCard_(lastRunLine_(last));
  } catch (err) {
    return homeCard_('⚠️ ' + err.message);
  }
}

function onSubmit(e) { return safeAction_('submit'); }
function onUpdate(e) { return safeAction_('update'); }

function onRefresh(e) {
  try {
    var p         = (e && e.parameters) || {};
    var cfg       = cfg_();
    var mode      = p.mode || '';
    var startedMs = Number(p.startedMs || Date.now());
    var runId     = p.runId ? Number(p.runId) : 0;
    var deadline  = Date.now() + POLL_BUDGET_MS;

    if (!runId) { // dispatched but the run was not visible yet — find it now
      var found = findRun_(cfg, startedMs);
      while (!found && Date.now() + 2000 < deadline) {
        Utilities.sleep(2000);
        found = findRun_(cfg, startedMs);
      }
      if (!found) {
        return nav_(runningCard_(mode, 0, startedMs,
                                 'Queued — run not visible yet'));
      }
      runId = found.id;
    }

    var run = pollUntil_(cfg, runId, deadline);
    if (run && run.status === 'completed') return nav_(resultCard_(mode, run));
    return nav_(runningCard_(mode, runId, startedMs,
                             run ? pretty_(run.status) : 'Running'));
  } catch (err) {
    return nav_(errorCard_(err));
  }
}

// ── Button flow ─────────────────────────────────────────────────────────────

function safeAction_(mode) {
  try {
    return nav_(dispatchAndPoll_(mode));
  } catch (err) {
    return nav_(errorCard_(err));
  }
}

function dispatchAndPoll_(mode) {
  var cfg       = cfg_();
  var startedMs = Date.now();
  var deadline  = startedMs + POLL_BUDGET_MS;

  dispatch_(cfg, mode); // HTTP 204, returns no run id

  // Locate the run this dispatch created: newest workflow_dispatch run of
  // our workflow created at/after dispatch time (small skew allowance).
  var run = null;
  while (!run && Date.now() + 2000 < deadline) {
    Utilities.sleep(2000);
    run = findRun_(cfg, startedMs);
  }
  if (!run) return runningCard_(mode, 0, startedMs, 'Queued');

  run = pollUntil_(cfg, run.id, deadline);
  if (run && run.status === 'completed') return resultCard_(mode, run);
  return runningCard_(mode, run ? run.id : 0, startedMs,
                      run ? pretty_(run.status) : 'Queued');
}

// ── GitHub API ──────────────────────────────────────────────────────────────

function cfg_() {
  var p   = PropertiesService.getScriptProperties();
  var cfg = {
    pat:      p.getProperty('GITHUB_PAT'),
    owner:    p.getProperty('GH_OWNER'),
    repo:     p.getProperty('GH_REPO'),
    workflow: p.getProperty('GH_WORKFLOW') || 'sync.yml',
    ref:      p.getProperty('GH_REF') || 'main'
  };
  if (!cfg.pat || !cfg.owner || !cfg.repo) {
    throw new Error('Set Script Properties GITHUB_PAT / GH_OWNER / GH_REPO ' +
                    '(see addon/README.md).');
  }
  return cfg;
}

function ghHeaders_(cfg) {
  return {
    Authorization: 'Bearer ' + cfg.pat,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28'
  };
}

function gh_(cfg, path) {
  var resp = UrlFetchApp.fetch(GITHUB_API + path, {
    headers: ghHeaders_(cfg),
    muteHttpExceptions: true
  });
  if (resp.getResponseCode() >= 300) {
    throw new Error('GitHub HTTP ' + resp.getResponseCode() + ' on ' + path);
  }
  return JSON.parse(resp.getContentText());
}

function dispatch_(cfg, mode) {
  var url  = GITHUB_API + '/repos/' + cfg.owner + '/' + cfg.repo +
             '/actions/workflows/' + cfg.workflow + '/dispatches';
  var resp = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: ghHeaders_(cfg),
    payload: JSON.stringify({ ref: cfg.ref, inputs: { mode: mode } }),
    muteHttpExceptions: true
  });
  if (resp.getResponseCode() !== 204) {
    throw new Error('Dispatch failed: HTTP ' + resp.getResponseCode() +
                    ' — ' + resp.getContentText().slice(0, 200));
  }
}

function findRun_(cfg, sinceMs) {
  var data = gh_(cfg, '/repos/' + cfg.owner + '/' + cfg.repo +
                      '/actions/runs?event=workflow_dispatch&per_page=10');
  var runs = data.workflow_runs || [];
  for (var i = 0; i < runs.length; i++) { // newest first
    var r = runs[i];
    if (r.path && r.path.indexOf(cfg.workflow) === -1) continue;
    // 15s skew allowance between our clock and GitHub's created_at.
    if (new Date(r.created_at).getTime() >= sinceMs - 15000) return r;
  }
  return null;
}

function getRun_(cfg, runId) {
  return gh_(cfg, '/repos/' + cfg.owner + '/' + cfg.repo +
                  '/actions/runs/' + runId);
}

function pollUntil_(cfg, runId, deadlineMs) {
  var run = getRun_(cfg, runId);
  while (run && run.status !== 'completed' &&
         Date.now() + POLL_INTERVAL_MS < deadlineMs) {
    Utilities.sleep(POLL_INTERVAL_MS);
    run = getRun_(cfg, runId);
  }
  return run;
}

function latestManualRun_(cfg) {
  var data = gh_(cfg, '/repos/' + cfg.owner + '/' + cfg.repo +
                      '/actions/runs?event=workflow_dispatch&per_page=1');
  return (data.workflow_runs || [])[0] || null;
}

function lastRunLine_(run) {
  if (!run) return 'No manual runs yet.';
  var mark = run.status !== 'completed' ? '⏳'
           : run.conclusion === 'success' ? '✅' : '❌';
  var when = Utilities.formatDate(new Date(run.created_at),
                                  Session.getScriptTimeZone(), 'MMM d, HH:mm');
  return 'Last manual run: ' + mark + ' ' +
         pretty_(run.status === 'completed' ? run.conclusion : run.status) +
         ' · ' + when;
}

// ── Cards ───────────────────────────────────────────────────────────────────

function oneButton_(text, fn) {
  // Each button gets its own ButtonSet (its own row); both are FILLED and
  // given equal-length labels so they render the same width.  CardService
  // exposes no width / height / padding control, so this even, solid pair is
  // the most balanced and prominent look the platform allows.
  return CardService.newButtonSet().addButton(
    CardService.newTextButton()
      .setText(text)
      .setTextButtonStyle(CardService.TextButtonStyle.FILLED)
      .setOnClickAction(CardService.newAction().setFunctionName(fn)));
}

function actionWidgets_() {
  return [
    oneButton_('Submit (~1 min)', 'onSubmit'),
    CardService.newTextParagraph().setText(' '),  // spacer → more separation
    oneButton_('Update (~2 min)', 'onUpdate'),
  ];
}

function baseCard_(statusHtml, opts) {
  opts = opts || {};
  var section = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph().setText(statusHtml));
  (opts.widgets || []).forEach(function (w) { section.addWidget(w); });
  // Submit/Update (and their explainer) show on the idle / done / error
  // cards, but NOT while a run is in progress — that card carries only the
  // Refresh button so a run can't be dispatched on top of itself.
  if (opts.showActions !== false) {
    actionWidgets_().forEach(function (w) { section.addWidget(w); });
    section.addWidget(CardService.newTextParagraph().setText(
      '<b>What these do</b><br>' +
      '• <b>Submit</b> (~1 min) — saves the promise dates you dragged onto the calendars<br>' +
      '• <b>Update</b> (~2 min) — pulls the latest balances and payments for units that changed<br>' +
      '<br>' +
      '<b>Checking the result</b><br>' +
      'After you press a button it runs on the server (about a minute). Then either:<br>' +
      '• press the <b>Refresh status</b> button that appears, or<br>' +
      '• close and reopen this panel — it always shows the latest run<br>' +
      '<br>' +
      '<i>The automatic hourly sync catches anything missed, so nothing here can break the calendars.</i>'));
  }
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader()
      .setTitle('OKPM Calendar Sync')
      .setSubtitle('AppFolio → owner calendars'))
    .addSection(section)
    .build();
}

function homeCard_(statusLine) {
  return baseCard_(statusLine || ' ', {});
}

function runningCard_(mode, runId, startedMs, statusText) {
  var since = Utilities.formatDate(new Date(startedMs),
                                   Session.getScriptTimeZone(), 'HH:mm:ss');
  var refresh = CardService.newTextButton()
    .setText('Refresh status')
    .setTextButtonStyle(CardService.TextButtonStyle.FILLED)
    .setOnClickAction(CardService.newAction()
      .setFunctionName('onRefresh')
      .setParameters({
        runId: String(runId || ''),
        mode: mode,
        startedMs: String(startedMs)
      }));
  var label = mode ? pretty_(mode) : 'Sync';
  return baseCard_(
    '⏳ <b>' + label + ' is running</b> (started ' + since + ').<br>' +
    'Usually done within a minute or two.<br>' +
    '<br>' +
    'To check on it:<br>' +
    '• press <b>Refresh status</b> below, or<br>' +
    '• close this panel and reopen it shortly<br>' +
    '<br>' +
    '<i>No need to keep it open or keep clicking — reopening always shows ' +
    'the current status.</i>',
    { widgets: [refresh], showActions: false });
}

function resultCard_(mode, run) {
  var label = mode ? pretty_(mode) : 'Sync';
  var text  = run.conclusion === 'success'
    ? '✅ <b>' + label + ' finished</b> — your changes are saved to the calendars.'
    : '❌ <b>' + label + ' did not finish cleanly.</b> Try again in a minute — ' +
      'the hourly sync will still catch it up.';
  return baseCard_(text, {});
}

function errorCard_(err) {
  return baseCard_('⚠️ ' + String(err && err.message || err), {});
}

// ── Small helpers ───────────────────────────────────────────────────────────

function nav_(card) {
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(card))
    .build();
}

function pretty_(s) {
  s = String(s || '');
  return s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, ' ');
}
