# -*- coding: utf-8 -*-
"""构建 Windows onedir 与 Inno Setup 安装包。

用法:
    python -m pip install -r packaging/requirements-build.txt
    python packaging/build_windows.py

输出:
    dist/ZCodeResumeWatcher/ZCodeResumeWatcher.exe
    dist/installer/ZCodeResumeWatcher-<version>-Setup.exe
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "packaging", "windows.spec")
ISS = os.path.join(ROOT, "packaging", "windows.iss")


def version():
    text = open(os.path.join(ROOT, "zcode_resume_watcher.py"),
                encoding="utf-8").read()
    match = re.search(r'^VERSION = "([\d.]+)"', text, re.M)
    if not match:
        raise SystemExit("找不到 VERSION")
    return match.group(1)


def find_iscc():
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local, "Programs", "Inno Setup 6", "ISCC.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""),
                     "Inno Setup 6", "ISCC.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""),
                     "Inno Setup 6", "ISCC.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return shutil.which("ISCC")


def run(cmd):
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def main():
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", SPEC])
    ver = version()
    iscc = find_iscc()
    if not iscc:
        print("未找到 Inno Setup 6 的 ISCC.exe；exe 已生成，安装包跳过。")
        print("安装: winget install --id JRSoftware.InnoSetup -e")
        return 0
    run([iscc, "/DMyAppVersion=%s" % ver, ISS])
    print("安装包: dist/installer/ZCodeResumeWatcher-%s-Setup.exe" % ver)
    return 0


if __name__ == "__main__":
    sys.exit(main())
