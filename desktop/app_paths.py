# -*- coding: utf-8 -*-
"""打包/源码两种运行方式下的路径解析。

- bundle_root: 只读应用资源（引擎源码、panel.html、图标、ps1）。
  源码运行 = 项目根；PyInstaller onedir/.app = sys._MEIPATH。
- data_root: 可写运行态（配置、日志、ui_state、panel_runtime、db）。
  源码运行 = 项目根；安装版 = 用户目录，避免写 Program Files/只读 .app。
"""
import os
import sys


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def bundle_root():
    if is_frozen():
        return os.path.abspath(
            getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_data_root():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ZCodeResumeWatcher")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", "ZCodeResumeWatcher")
    return os.path.join(os.path.expanduser("~"), ".local", "share",
                        "zcode-resume-watcher")


def ensure_data_root(path):
    os.makedirs(path, exist_ok=True)
    for name in ("zcode-log", "rollout"):
        os.makedirs(os.path.join(path, name), exist_ok=True)
    return path
