# -*- mode: python ; coding: utf-8 -*-
"""Windows PyInstaller onedir 构建。

输出: dist/ZCodeResumeWatcher/ZCodeResumeWatcher.exe
引擎源码、ps1、面板、图标作为 data 文件随包分发;运行态写 %LOCALAPPDATA%。
"""
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "zcode_resume_watcher.py"), "."),
    (os.path.join(ROOT, "config.json"), "."),
    (os.path.join(ROOT, "uia_focus_input.ps1"), "."),
    (os.path.join(ROOT, "uia_navigate_input.ps1"), "."),
    (os.path.join(ROOT, "desktop", "webui", "panel.html"), "desktop/webui"),
    (os.path.join(ROOT, "desktop", "assets"), "desktop/assets"),
]

a = Analysis(
    [os.path.join(ROOT, "desktop", "app_entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=["desktop.tray_win", "zcode_resume_watcher"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ZCodeResumeWatcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(ROOT, "desktop", "assets", "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ZCodeResumeWatcher",
)
