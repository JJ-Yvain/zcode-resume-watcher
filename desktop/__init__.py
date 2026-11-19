# -*- coding: utf-8 -*-
"""ZCode 续命监视器桌面壳。

分层:
- ``engine_host``:导入单文件引擎并按需隔离路径,不改判定逻辑;
- ``web_runtime``:把 watcher.log 重建成运行区间/恢复记录/会话;
- ``web_server``:127.0.0.1 随机端口 + token 的本地面板服务;
- ``web_app``:引擎线程 + 面板服务 + 托盘 + 单实例激活;
- ``tray_win``:ctypes 托盘(Windows);macOS v1 走 .command + 信号退出;
- ``platform_services``:系统能力唯一出口(Edge app 窗/打开文件/消息框)。
"""

__all__ = []
