# -*- mode: python ; coding: utf-8 -*-
"""macOS PyInstaller .app 构建。

输出: dist/ZCode 续命监视器.app (LSUIElement 菜单栏应用)
必须在 macOS 上构建; PyInstaller 不支持交叉编译。
"""
import os

from PyInstaller.utils.hooks import collect_submodules

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
    hiddenimports=["desktop.mac_menu", "zcode_resume_watcher"]
    + collect_submodules("rumps"),
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
    icon=os.path.join(ROOT, "desktop", "assets", "app.icns"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ZCodeResumeWatcher",
)

app = BUNDLE(
    coll,
    name="ZCode 续命监视器.app",
    icon=os.path.join(ROOT, "desktop", "assets", "app.icns"),
    bundle_identifier="com.zcode.resumewatcher",
    info_plist={
        "CFBundleDisplayName": "ZCode 续命监视器",
        "CFBundleName": "ZCode Resume Watcher",
        "CFBundleShortVersionString": "1.2.0",
        "CFBundleVersion": "1.2.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
