# -*- coding: utf-8 -*-
"""桌面 app 组装:引擎线程 + 本地面板服务 + 托盘 + 单实例激活。

进程边界:
- 引擎线程:原有 run_watcher,不改判定/守卫;只是多了 stop_event。
- HTTP 线程:127.0.0.1 随机端口 + 每次启动随机 token,仅服务面板。
- 主线程:Windows 跑托盘消息循环;macOS v1 跑 signal 等待循环。
- Edge --app 是独立进程;关掉窗口不等于退出 app,托盘可再次打开。
"""
import argparse
import hashlib
import json
import os
import secrets
import signal
import sys
import threading
import time
import urllib.request

from . import platform_services
from .engine_host import configure_engine, import_engine
from .web_runtime import WebRuntime
from .web_server import PanelServer, UIState

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "desktop", "webui", "panel.html")
ASSETS = os.path.join(ROOT, "desktop", "assets")
RUNTIME_FILE = "panel_runtime.json"


def _say(text):
    """pythonw 下 sys.stdout 为 None;控制台输出只是开发便利。"""
    try:
        if sys.stdout is not None:
            print(text, flush=True)
    except Exception:
        pass


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _ping(record, timeout=0.8):
    """确认运行时文件里的端口仍是本 app(而不是被别的程序复用的端口)。"""
    try:
        port = int(record.get("port"))
        token = str(record.get("token") or "")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/ping" % port,
            headers={"X-Token": token})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        return bool(data.get("ok")) and int(data.get("pid", -1)) == int(record.get("pid", -2))
    except Exception:
        return False


def _activate(record):
    # 已有面板窗就唤回,不要每次二次启动都再开一个 Edge 窗口。
    if platform_services.focus_panel_window():
        return True
    url = record.get("url")
    if not url:
        return False
    return platform_services.open_edge_app(url, 1440, 900)


class DesktopApp:
    def __init__(self, root=ROOT, test_root=None, no_open=False, port=0,
                 no_tray=False):
        self.root = os.path.abspath(root)
        self.data_root = os.path.abspath(test_root) if test_root else self.root
        self.no_open = no_open
        self.no_tray = no_tray
        self.port = port
        self.is_test = bool(test_root)
        self.runtime_file = os.path.join(self.data_root, RUNTIME_FILE)
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.quit_event = threading.Event()
        self.engine = None
        self.runtime = None
        self.server = None
        self.tray = None
        self.engine_thread = None
        self.mutex = None
        self.engine_error = ""
        self._shut = False
        self._url = ""
        self._last_open = 0.0
        self._panel_hwnd = None
        self.ui_state = None
        self._window_size = (1440, 900)
        self._size_stop = threading.Event()
        self._size_thread = None
        self._size_ignore_until = 0.0

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def _mutex_name(self):
        override = os.environ.get("ZRW_MUTEX_NAME")
        if override:
            return override
        if self.is_test:
            digest = hashlib.sha1(self.data_root.encode("utf-8")).hexdigest()[:8]
            return r"Local\ZCodeResumeWatcherTest-" + digest
        return r"Local\ZCodeResumeWatcher"

    def _existing_instance(self):
        record = _read_json(self.runtime_file)
        if not record:
            return None
        if _ping(record):
            return record
        try:
            os.remove(self.runtime_file)
        except OSError:
            pass
        return None

    def start(self):
        if not os.path.exists(PANEL):
            platform_services.message_box(
                "ZCode 续命监视器",
                "面板文件缺失：\n%s\n\n请先运行 原型/_src/build.py。" % PANEL,
                error=True)
            return False

        existing = self._existing_instance()
        if existing is not None:
            if not self.no_open:
                _activate(existing)
            return False

        if self.is_test:
            os.makedirs(self.data_root, exist_ok=True)
            for d in ("zcode-log", "rollout"):
                os.makedirs(os.path.join(self.data_root, d), exist_ok=True)
            try:
                open(os.path.join(self.data_root, "watcher.log"),
                     "a", encoding="utf-8").close()
            except OSError:
                pass

        self.engine = import_engine(self.root)
        self.mutex = self.engine.acquire_single_instance(self._mutex_name())
        if self.mutex is None:
            again = self._existing_instance()
            if again is not None:
                if not self.no_open:
                    _activate(again)
                return False
            platform_services.message_box(
                "ZCode 续命监视器",
                "监视器已在运行。\n\n"
                "· 面板版：从系统托盘图标打开面板；\n"
                "· 控制台版：请先关闭黑色窗口，再启动面板。",
                error=False)
            return False

        overrides = {}
        if self.is_test:
            overrides = {
                "log_file": os.path.join(self.data_root, "watcher.log"),
                "log_dir": os.path.join(self.data_root, "zcode-log"),
                "rollout_dir": os.path.join(self.data_root, "rollout"),
                "db_path": os.path.join(self.data_root, "db.sqlite"),
            }
        configure_engine(self.engine, self.root,
                         config_path=self._config_path(), **overrides)

        self.runtime = WebRuntime(
            version=self.engine.VERSION,
            message=self.engine.MESSAGE,
            resolver=self.engine.resolve_session_target,
            breaker_limit=self.engine.CIRCUIT_BREAKER_LIMIT,
        )
        self.runtime.read_only = False
        self.runtime.seed_from_log(self.engine.LOG_FILE)
        self.engine.set_log_observer(self.runtime.observe)
        self.engine.set_pause_event(self.pause_event)
        if getattr(self.engine, "CHECK_UPDATES", False):
            threading.Thread(target=self.engine.check_updates,
                             name="update-check", daemon=True).start()

        self.ui_state = UIState(os.path.join(self.data_root, "ui_state.json"))
        saved = self.ui_state.window_size()
        if saved:
            self._window_size = saved

        token = secrets.token_hex(16)
        self.server = PanelServer(
            runtime=self.runtime,
            panel_path=PANEL,
            token=token,
            port=self.port,
            read_only=False,
            actions={
                "pause": self.pause,
                "resume": self.resume,
                "test_send": self.test_send,
                "open_log": self.open_log,
                "open_config": self.open_config,
            },
            ui_state=self.ui_state,
        )
        self.server.start()
        self._url = self.server.url()
        _atomic_write_json(self.runtime_file, {
            "pid": os.getpid(),
            "port": self.server.port,
            "token": token,
            "url": self._url,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })

        self.engine_thread = threading.Thread(
            target=self._engine_main, name="watcher-engine", daemon=True)
        self.engine_thread.start()
        self._size_thread = threading.Thread(
            target=self._watch_window_size, name="panel-size", daemon=True)
        self._size_thread.start()
        if not self.no_open:
            # 上一轮如果是强杀/崩溃,遗留的 Edge 窗会一直显示断连;先清掉再开新窗。
            platform_services.close_panel_windows()
            platform_services.open_edge_app(
                self._url, self._window_size[0], self._window_size[1],
                on_window=self._remember_panel)
        return True

    def _remember_panel(self, hwnd):
        self._panel_hwnd = int(hwnd)
        # 新窗刚打开时 Edge 还会做恢复/二次 fit;2.5s 内不把程序尺寸当用户偏好。
        self._size_ignore_until = time.monotonic() + 2.5

    def _watch_window_size(self):
        """拖拽过程中就持续记录最后一次尺寸;最小化/最大化不记。

        不做 1s 稳定等待:用户拖完可能马上关闭窗口,等稳定会漏掉最后一次尺寸。
        """
        while not self._size_stop.wait(0.25):
            hwnd = self._panel_hwnd
            if not hwnd or time.monotonic() < self._size_ignore_until:
                continue
            size = platform_services.window_size(hwnd)
            if not size or size == self._window_size:
                continue
            if self.ui_state is not None and self.ui_state.set_window_size(*size):
                self._window_size = size

    def _config_path(self):
        if self.is_test:
            candidate = os.path.join(self.data_root, "config.json")
            if os.path.exists(candidate):
                return candidate
        return os.path.join(self.root, "config.json")

    def _engine_main(self):
        args = argparse.Namespace(
            interval=self.engine.POLL_SECONDS, dry_run=False, debug=False)
        try:
            self.engine.run_watcher(args, self.stop_event)
        except Exception as exc:
            self.engine_error = "%s: %s" % (type(exc).__name__, exc)
            try:
                self.engine._note_exit(
                    "桌面引擎未捕获异常 %s: %s(完整 traceback 见下)"
                    % (type(exc).__name__, exc))
                self.engine._dump_traceback()
            except Exception:
                pass
            self.runtime.set_state("error", self.engine_error)
        else:
            if not self._shut:
                self.runtime.set_state("stopped")

    # ------------------------------------------------------------------
    # 面板/托盘动作
    # ------------------------------------------------------------------
    def display_state(self):
        if self.engine_error:
            return "error"
        if self.pause_event.is_set():
            return "paused"
        return self.runtime.state if self.runtime else "starting"

    def pause(self):
        if self.engine_error:
            return False
        self.pause_event.set()
        if self.runtime:
            self.runtime.set_state("paused")
        return True

    def resume(self):
        if self.engine_error:
            return False
        self.pause_event.clear()
        alive = self.engine_thread is not None and self.engine_thread.is_alive()
        if self.runtime:
            self.runtime.set_state("running" if alive else "stopped")
        return alive

    def toggle_pause(self):
        return self.resume() if self.pause_event.is_set() else self.pause()

    def test_send(self):
        if self.engine is None or self.engine_error:
            return False
        try:
            result = self.engine.test_send_once(delay=0, announce=False)
        except Exception:
            return False
        return bool(result)

    def test_send_confirmed(self):
        """托盘入口:先弹原生确认,再在后台发送;不阻塞托盘消息循环。"""
        def work():
            if platform_services.ask_yes_no(
                    "测试发送",
                    "向当前 ZCode 会话投递一条测试消息？\n\n"
                    "只确认文字已入队，不核验送达。"):
                self.test_send()
        threading.Thread(target=work, daemon=True).start()

    def open_log(self):
        return platform_services.open_path(self.engine.LOG_FILE)

    def open_config(self):
        return platform_services.open_path(self._config_path())

    def open_panel(self):
        if not self._url:
            return False
        # 已有窗口就唤回;没有才新开。托盘双击也只会得到一个窗口。
        hwnd = platform_services.focus_panel_window()
        if hwnd:
            self._panel_hwnd = int(hwnd)
            return True
        now = time.monotonic()
        if now - self._last_open < 1.0:
            return True
        self._last_open = now
        return platform_services.open_edge_app(
            self._url, self._window_size[0], self._window_size[1],
            on_window=self._remember_panel)

    def request_quit(self):
        self.quit_event.set()
        if self.tray is not None:
            self.tray.stop()

    # ------------------------------------------------------------------
    # 运行 / 退出
    # ------------------------------------------------------------------
    def run(self):
        if not self.start():
            return 0
        _say("面板: %s" % self._url)
        if sys.platform == "win32" and not self.no_tray:
            _say("托盘: 右键菜单可暂停/测试/退出; 关掉 Edge 窗口不会退出引擎")
        else:
            _say("无托盘模式: Ctrl+C / SIGTERM 退出; 关掉面板窗口不会退出引擎")
        if sys.platform == "win32" and not self.no_tray:
            from .tray_win import TrayIcon
            self.tray = TrayIcon(
                callbacks={
                    "open": self.open_panel,
                    "toggle_pause": self.toggle_pause,
                    "test_send": self.test_send_confirmed,
                    "open_log": self.open_log,
                    "open_config": self.open_config,
                    "quit": self.request_quit,
                },
                snapshot=lambda: {"state": self.display_state()},
                assets_dir=ASSETS,
            )
            if not self.tray.run():
                platform_services.message_box(
                    "ZCode 续命监视器",
                    "托盘初始化失败，无法在后台安全驻留。\n"
                    "引擎未启动；请检查系统托盘/资源管理器后重试。",
                    error=True)
                self.shutdown()
                return 1
        else:
            for name in ("SIGTERM", "SIGINT", "SIGHUP"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    try:
                        signal.signal(sig, lambda *_: self.request_quit())
                    except (OSError, ValueError):
                        pass
            while not self.quit_event.wait(0.5):
                pass
        self.shutdown()
        return 0

    def shutdown(self):
        if self._shut:
            return
        self._shut = True
        self.pause_event.set()
        self.stop_event.set()
        self._size_stop.set()
        if self._size_thread is not None:
            self._size_thread.join(timeout=1.0)
        if self.engine is not None:
            self.engine.wait_for_send_idle(30.0)
        if self.engine_thread is not None:
            self.engine_thread.join(timeout=5.0)
        if self.engine is not None:
            try:
                self.engine._note_exit("桌面面板退出")
            except Exception:
                pass
        if self.runtime is not None:
            self.runtime.set_state("stopped")
        if self.server is not None:
            self.server.stop()
        # Edge app 窗是独立进程:只关本实例记录的那一个窗,避免误关别的实例/浏览器窗。
        try:
            if self._panel_hwnd is None and not self.no_open:
                # 冷启动时 Edge 可能还没建好窗;给回调 2s 落地机会。
                for _ in range(20):
                    if self._panel_hwnd:
                        break
                    time.sleep(0.1)
            if self._panel_hwnd:
                platform_services.close_panel_window(self._panel_hwnd)
        except Exception:
            pass
        record = _read_json(self.runtime_file)
        if record and int(record.get("pid", -1)) == os.getpid():
            try:
                os.remove(self.runtime_file)
            except OSError:
                pass
        if self.engine is not None and self.mutex is not None:
            self.engine.release_single_instance(self.mutex)
            self.mutex = None


def run_demo(no_open=False, port=0):
    """静态原型预览:假数据、不过引擎、不占互斥体。仅用于设计验收。"""
    panel = os.path.join(ROOT, "原型", "01-recovery-console", "recovery-console.html")
    if not os.path.exists(panel):
        panel = PANEL
    runtime = WebRuntime(version="demo", message="任务意外中断，请继续！")
    runtime.read_only = True
    runtime.set_state("running")
    token = secrets.token_hex(16)
    server = PanelServer(runtime=runtime, panel_path=panel, token=token,
                         port=port, read_only=True)
    server.start()
    url = server.url()
    _say("演示面板: %s" % url)
    if not no_open:
        platform_services.open_edge_app(url, 1440, 900)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="ZCode 续命监视器 · 桌面面板")
    ap.add_argument("--demo", action="store_true", help="静态原型预览(假数据)")
    ap.add_argument("--test-root", metavar="DIR", help="隔离测试根目录(换互斥体/日志)")
    ap.add_argument("--no-open", action="store_true", help="不自动打开面板窗口")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-tray", action="store_true", help="不建托盘(隔离测试/调试)")
    ap.add_argument("--test-seconds", type=float, default=0,
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    platform_services.set_app_user_model_id()
    if args.demo:
        return run_demo(no_open=args.no_open, port=args.port)
    app = DesktopApp(test_root=args.test_root, no_open=args.no_open,
                     port=args.port, no_tray=args.no_tray)
    if args.test_seconds > 0:
        threading.Timer(args.test_seconds, app.request_quit).start()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
