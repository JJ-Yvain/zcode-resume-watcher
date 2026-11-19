# -*- coding: utf-8 -*-
"""ZCode 续命监视器 · 桌面面板无窗入口。

双击本文件（或由启动 bat 调用 pythonw）不会出现黑窗口。
启动失败时写 desktop-crash.log，并尽量弹原生消息框。
"""
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _crash(message):
    try:
        with open(os.path.join(ROOT, "desktop-crash.log"), "a",
                  encoding="utf-8") as f:
            f.write(message + "\n")
    except OSError:
        pass
    try:
        from desktop.platform_services import message_box
        message_box("ZCode 续命监视器", message[-1800:], error=True)
    except Exception:
        pass


def main():
    try:
        from desktop.web_app import main as app_main
        return app_main()
    except SystemExit:
        raise
    except Exception:
        _crash("桌面面板启动失败\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
