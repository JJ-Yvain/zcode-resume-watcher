# -*- coding: utf-8 -*-
"""只读实时预览：把真实 watcher.log 喂给 A 面板，不启动引擎、不写日志。

用途：在接引擎缝之前，验证面板能吃真实数据、四段链路能填满、亮暗主题正常。

    python desktop/web_preview.py            # 打开 Edge 应用窗口
    python desktop/web_preview.py --no-open  # 只启动服务，打印 URL
"""
import argparse
import os
import secrets
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop import platform_services                      # noqa: E402
from desktop.engine_host import load_engine                # noqa: E402
from desktop.web_runtime import WebRuntime                 # noqa: E402
from desktop.web_server import PanelServer, UIState        # noqa: E402


class LogFollower(threading.Thread):
    """从文件末尾开始尾随新增行；文件被截断时从头重读。"""

    def __init__(self, path, on_line, stop_event):
        super().__init__(name="log-follower", daemon=True)
        self.path = path
        self.on_line = on_line
        self.stop_event = stop_event
        self._offset = 0
        self._fh = None

    def _open_at_end(self):
        self._close()
        try:
            self._fh = open(self.path, "r", encoding="utf-8",
                            errors="replace")
            self._fh.seek(0, os.SEEK_END)
            self._offset = self._fh.tell()
        except OSError:
            self._fh = None
            self._offset = 0

    def _close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def run(self):
        self._open_at_end()
        while not self.stop_event.is_set():
            if self._fh is None:
                self._open_at_end()
                if self._fh is None:
                    self.stop_event.wait(1.0)
                    continue
            try:
                line = self._fh.readline()
            except (OSError, ValueError):
                self._close()
                continue
            if line:
                self.on_line(line)
                continue
            # 无新行：检查截断/轮转
            try:
                size = os.path.getsize(self.path)
            except OSError:
                size = 0
            if size < self._offset:
                self._open_at_end()
                continue
            self.stop_event.wait(0.5)
        self._close()

    def stop(self):
        self.stop_event.set()


def build_runtime(engine):
    runtime = WebRuntime(version=engine.VERSION,
                         message=engine.MESSAGE,
                         resolver=engine.resolve_session_target,
                         breaker_limit=engine.CIRCUIT_BREAKER_LIMIT)
    runtime.read_only = True
    runtime.seed_from_log(engine.LOG_FILE)
    return runtime


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读实时预览")
    parser.add_argument("--no-open", action="store_true",
                        help="不自动打开 Edge 窗口")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)

    engine = load_engine(str(ROOT), suppress_config_log=True)
    runtime = build_runtime(engine)
    stop_event = threading.Event()

    panel = ROOT / "desktop" / "webui" / "panel.html"
    if not panel.exists():
        print("缺少面板文件，先运行 python 原型/_src/build.py")
        return 2

    def open_log():
        return platform_services.open_path(engine.LOG_FILE)

    def open_config():
        return platform_services.open_path(str(ROOT / "config.json"))

    token = secrets.token_hex(16)
    server = PanelServer(
        runtime=runtime,
        panel_path=str(panel),
        token=token,
        port=args.port,
        read_only=True,
        actions={"open_log": open_log, "open_config": open_config},
        ui_state=UIState(str(ROOT / "ui_state.json")),
    )
    server.start()
    follower = LogFollower(engine.LOG_FILE, runtime.observe, stop_event)
    follower.start()

    url = server.url()
    print("预览地址: %s" % url)
    print("数据源: %s （只读，不写日志）" % engine.LOG_FILE)
    print("Ctrl+C 退出")
    if not args.no_open:
        platform_services.open_edge_app(url, 1440, 900)

    try:
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        follower.join(timeout=2.0)
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
