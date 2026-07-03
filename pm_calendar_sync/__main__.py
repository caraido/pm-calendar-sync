"""``python -m pm_calendar_sync`` entry point — RUN_MODE dispatch.

The GitHub Actions workflow sets ``RUN_MODE`` (from the cron schedule or the
workflow_dispatch input) and runs ``python sync.py``, which delegates here:

  full_nightly  (default) full sweep + directory cache/ACL refresh
  full          full sweep from cached directories, no ACL calls
  update        changed-units-only fast path
  submit        drag/commitment-only fast path

``update``/``submit`` fall back to a plain full sweep (with a warning) until
their implementations exist — a dispatch can never crash on a missing mode.
"""
import os

from .config import log
from .orchestrator import SyncOrchestrator


def main():
    mode = os.environ.get("RUN_MODE", "full_nightly").strip().lower()
    orch = SyncOrchestrator()
    if mode == "update" and hasattr(orch, "run_update"):
        orch.run_update()
    elif mode == "submit" and hasattr(orch, "run_submit"):
        orch.run_submit()
    elif mode in ("full", "full_nightly"):
        orch.run(mode)
    else:
        log.warning(f"RUN_MODE={mode!r} not available — falling back to full sweep")
        orch.run("full")


if __name__ == "__main__":
    main()
