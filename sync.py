#!/usr/bin/env python3
"""
OKPM AppFolio → Google Calendar Sync — entry point shim.

The implementation lives in the ``pm_calendar_sync`` package.  This shim
keeps ``python sync.py`` — the command the GitHub Actions workflow runs —
working.  The run mode is selected by the ``RUN_MODE`` env var (set by the
workflow from the cron schedule / dispatch input); see
``pm_calendar_sync/__main__.py`` for the dispatch table.
"""
from pm_calendar_sync.__main__ import main

if __name__ == "__main__":
    main()
