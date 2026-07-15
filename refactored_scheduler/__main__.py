"""允许使用 ``python -m refactored_scheduler`` 启动调度器。"""

from .main import main


if __name__ == "__main__":
    raise SystemExit(main())
