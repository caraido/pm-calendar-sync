#!/usr/bin/env python3
"""
OKPM AppFolio → Google Calendar Sync — entry point shim.

The implementation now lives in the ``pm_calendar_sync`` package (a
conservative modular split of what used to be this single file). This shim
keeps ``python sync.py`` — the command the GitHub Actions workflow runs —
working exactly as before. The logic is unchanged; see
``pm_calendar_sync/orchestrator.py``.
"""
from pm_calendar_sync.orchestrator import SyncOrchestrator

if __name__ == "__main__":
    SyncOrchestrator().run()
