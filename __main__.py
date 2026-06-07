"""`python -m pm_calendar_sync` entry point."""
from .pm_calendar_sync.orchestrator import SyncOrchestrator


def main():
    SyncOrchestrator().run()


if __name__ == "__main__":
    main()
