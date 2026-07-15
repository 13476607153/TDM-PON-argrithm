"""PyCharm/command-line entry point for the refactored TS-DetBA scheduler."""

try:
    from .main import main
except ImportError:  # 兼容在项目父目录已加入 sys.path 时直接运行本文件。
    from refactored_scheduler.main import main


def run_refactored_scheduler():
    """Run the refactored scheduler and return its process-style exit code."""
    return main()


if __name__ == "__main__":
    raise SystemExit(run_refactored_scheduler())
