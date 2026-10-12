"""向后兼容的入口——默认启动 GUI 界面。

实际逻辑在 ``boss_zhipin.tauri`` ；本文件只是把 if-name-main 那段委托过去。
如果需要运行 CLI，请使用 ``uv run boss-zhipin`` 或 ``uv run python -m boss_zhipin.cli``。
"""

from boss_zhipin.tauri import main

if __name__ == "__main__":
    import sys
    sys.exit(main())