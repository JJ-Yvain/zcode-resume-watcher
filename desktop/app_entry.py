# -*- coding: utf-8 -*-
"""PyInstaller 入口：源码运行与打包运行共用同一套 DesktopApp。"""
import sys

from desktop.web_app import main

if __name__ == "__main__":
    sys.exit(main())
