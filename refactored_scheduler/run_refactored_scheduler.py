"""PyCharm/command-line entry point for the refactored TS-DetBA scheduler."""

from refactored_scheduler.main import main


def run_refactored_scheduler():
    """Run the refactored scheduler and return its process-style exit code."""
    return main()


if __name__ == "__main__":
    raise SystemExit(run_refactored_scheduler())
