# -*- coding: utf-8 -*-
"""桌面壳接缝测试:日志观察者 / 暂停闸门 / 停止信号 / Web 运行时 / 面板 API / app 生命周期。

不启动真实引擎事件、不发送、不碰真实 watcher.log;全部在 tmp/ 隔离目录内。
运行: python -m unittest tests.web_shell.test_web_shell -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.engine_host import import_engine            # noqa: E402
from desktop.web_runtime import WebRuntime               # noqa: E402
from desktop.web_server import PanelServer               # noqa: E402


def _args(interval=0.05):
    class A:
        dry_run = False
        debug = False

    a = A()
    a.interval = interval
    return a


class WebShellTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(ROOT / "tmp", exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="zrw-web-", dir=str(ROOT / "tmp")))
        self.engine = import_engine(str(ROOT))
        self.engine.LOG_FILE = str(self.tmp / "watcher.log")
        self.engine.NAV_LOG = str(self.tmp / "navigate.log")
        self.engine.LOG_DIR = str(self.tmp / "zcode-log")
        os.makedirs(self.engine.LOG_DIR, exist_ok=True)
        self._saved_stdout = sys.stdout

    def tearDown(self):
        sys.stdout = self._saved_stdout
        self.engine.set_log_observer(None)
        self.engine.set_pause_event(None)
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # ---- 引擎接缝 ----
    def test_log_observer_and_safe_print(self):
        seen = []

        class Boom:
            def write(self, *_a):
                raise UnicodeEncodeError("gbk", "x", 0, 1, "boom")

            def flush(self):
                raise UnicodeEncodeError("gbk", "x", 0, 1, "boom")

        sys.stdout = Boom()
        self.engine.set_log_observer(seen.append)
        self.engine.log("观察者 hello")
        text = Path(self.engine.LOG_FILE).read_text(encoding="utf-8")
        self.assertIn("观察者 hello", text)
        self.assertTrue(any("观察者 hello" in line for line in seen))

    def test_pause_gate_blocks_send(self):
        self.engine.PAUSE_EVENT = threading.Event()
        self.engine.PAUSE_EVENT.set()
        ok = self.engine.trigger_resume(
            "connection", {"sessionId": "sess_pause_test"}, _args())
        text = Path(self.engine.LOG_FILE).read_text(encoding="utf-8")
        self.assertFalse(ok)
        self.assertIn("已暂停,跳过自动发送", text)

    @unittest.skipUnless(sys.platform == "win32",
                         "Windows test-send path; macOS uses osascript")
    def test_test_send_without_window_returns_none(self):
        saved = self.engine.find_zcode_hwnd
        self.engine.find_zcode_hwnd = lambda: None
        try:
            self.assertIsNone(self.engine.test_send_once(delay=0, announce=False))
        finally:
            self.engine.find_zcode_hwnd = saved

    def test_stop_event_exits_loop(self):
        self.engine.QUOTA_ENABLED = False
        self.engine.STALL_ENABLED = False
        self.engine.HEARTBEAT_MINUTES = 0
        ev = threading.Event()
        th = threading.Thread(target=self.engine.run_watcher,
                              args=(_args(0.02), ev), daemon=True)
        th.start()
        time.sleep(0.2)
        ev.set()
        th.join(2.0)
        self.assertFalse(th.is_alive())
        text = Path(self.engine.LOG_FILE).read_text(encoding="utf-8")
        self.assertIn("监视器启动", text)
        self.assertIn("主循环已停止", text)

    @unittest.skipUnless(sys.platform == "win32", "Windows mutex only")
    def test_mutex_name_is_configurable(self):
        name = r"Local\ZRWWebShellTest-" + uuid.uuid4().hex
        h1 = self.engine.acquire_single_instance(name)
        self.assertIsNotNone(h1)
        try:
            self.assertIsNone(self.engine.acquire_single_instance(name))
        finally:
            self.engine.release_single_instance(h1)
        h2 = self.engine.acquire_single_instance(name)
        self.assertIsNotNone(h2)
        self.engine.release_single_instance(h2)

    def test_wait_for_send_idle(self):
        self.engine._SEND_LOCK.acquire()
        result = []
        th = threading.Thread(
            target=lambda: result.append(self.engine.wait_for_send_idle(0.15)))
        th.start()
        th.join(2.0)
        self.assertEqual(result, [False])
        self.engine._SEND_LOCK.release()
        self.assertTrue(self.engine.wait_for_send_idle(0.5))

    @unittest.skipUnless(sys.platform == "win32", "Windows window class test")
    def test_panel_window_filter_excludes_tray_window(self):
        """托盘隐藏消息窗与面板同名;窗口枚举必须按 class 排除,否则会显示空白黑窗。"""
        import ctypes
        import ctypes.wintypes as wt
        from desktop import platform_services, tray_win
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.RegisterClassW.argtypes = [ctypes.POINTER(tray_win.WNDCLASSW)]
        user32.RegisterClassW.restype = wt.ATOM
        user32.CreateWindowExW.argtypes = [
            wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
            wt.HINSTANCE, ctypes.c_void_p]
        user32.CreateWindowExW.restype = wt.HWND
        user32.DestroyWindow.argtypes = [wt.HWND]
        user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t

        def proc(hwnd, msg, wparam, lparam):
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        cb = tray_win._WNDPROC(proc)
        wc = tray_win.WNDCLASSW()
        wc.lpfnWndProc = cb
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "ZCodeResumeWatcherTrayWindow"
        atom = user32.RegisterClassW(ctypes.byref(wc))
        self.assertTrue(atom)
        hwnd = user32.CreateWindowExW(
            0, "ZCodeResumeWatcherTrayWindow", "ZCode 续命监视器", 0,
            0, 0, 0, 0, None, None, wc.hInstance, None)
        self.assertTrue(hwnd)
        try:
            self.assertNotIn(
                int(hwnd),
                platform_services._panel_windows("ZCode 续命监视器"))
        finally:
            user32.DestroyWindow(hwnd)

    # ---- Web 运行时 ----
    def test_runtime_rebuilds_four_stages(self):
        log = self.tmp / "history.log"
        log.write_text("\n".join([
            "2026-11-19 10:00:00 监视器启动 (平台: Windows, PID 1, 轮询 1.0s, live)",
            "2026-11-19 10:05:00 归位成功(顶栏标题验证): 「测试会话」",
            "2026-11-19 10:05:02 已自动发送并确认送达 '任务意外中断，请继续！' "
            "(connection, sess_test0001-aaaa-bbbb-cccc-dddd00000001)",
            "2026-11-19 10:10:00 熔断中,跳过自动发送 "
            "(会话 sess_test0001-aaaa-bbbb-cccc-dddd00000001, 连续 5 次未恢复,等 turn.completed 复位)",
            "2026-11-19 10:20:00 ⚠ 监视器退出: 测试退出 (PID 1)",
        ]) + "\n", encoding="utf-8")
        rt = WebRuntime(version="t", breaker_limit=5)
        rt.seed_from_log(str(log))
        snap = rt.snapshot()
        self.assertEqual(snap["state"], "stopped")
        self.assertFalse(snap["runs"][0]["current"])
        self.assertEqual(len(snap["recoveries"]), 2)
        rec = [r for r in snap["recoveries"] if r["outcome"] == "delivered"][0]
        self.assertEqual([s["status"] for s in rec["stages"]],
                         ["done", "done", "done", "done"])
        self.assertEqual(rec["stages"][1]["detail"],
                         "侧栏定位 · 顶栏标题逐字验证通过")
        guard = [r for r in snap["recoveries"] if r["outcome"] == "guarded"][0]
        self.assertEqual(guard["trigger"]["kind"], "breaker")
        self.assertEqual(snap["sessions"][0]["state"], "blocked")

    def test_runtime_empty_log_is_first_run(self):
        rt = WebRuntime()
        rt.seed_from_log(str(self.tmp / "missing.log"))
        snap = rt.snapshot()
        self.assertTrue(snap["firstRun"])
        self.assertEqual(snap["state"], "starting")

    # ---- 面板 API ----
    def _server(self, read_only=False):
        rt = WebRuntime()
        rt.set_state("running")
        actions = {
            "pause": lambda: True,
            "resume": lambda: True,
            "test_send": lambda: False,
        }
        srv = PanelServer(
            runtime=rt,
            panel_path=str(ROOT / "desktop" / "webui" / "panel.html"),
            token="tok-test",
            read_only=read_only,
            actions=actions,
        )
        srv.start()
        return srv

    def _req(self, srv, path, token="tok-test", method="GET"):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (srv.port, path), method=method,
            data=b"{}" if method == "POST" else None,
            headers={"X-Token": token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)

    def test_panel_api_auth_and_results(self):
        srv = self._server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._req(srv, "/api/state", token="wrong")
            self.assertEqual(ctx.exception.code, 401)
            code, data = self._req(srv, "/api/state")
            self.assertEqual(code, 200)
            self.assertEqual(data["state"], "running")
            code, data = self._req(srv, "/api/pause", method="POST")
            self.assertTrue(data["ok"])
            code, data = self._req(srv, "/api/test-send", method="POST")
            self.assertFalse(data["ok"])
        finally:
            srv.stop()

    def test_panel_read_only_rejects_actions(self):
        srv = self._server(read_only=True)
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._req(srv, "/api/pause", method="POST")
            self.assertEqual(ctx.exception.code, 409)
        finally:
            srv.stop()

    # ---- app 生命周期 ----
    @unittest.skipUnless(sys.platform == "win32",
                         "桌面 app 生命周期在 Windows 上验收;macOS 真机手动冒烟")
    def test_app_start_pause_resume_shutdown(self):
        from desktop.web_app import DesktopApp
        old = os.environ.get("ZRW_MUTEX_NAME")
        os.environ["ZRW_MUTEX_NAME"] = r"Local\ZRWWebApp-" + uuid.uuid4().hex
        app_root = self.tmp / "app"
        os.makedirs(str(app_root), exist_ok=True)
        (app_root / "ui_state.json").write_text(
            json.dumps({"window": {"width": 1100, "height": 700}}),
            encoding="utf-8")
        app = DesktopApp(test_root=str(app_root), no_open=True,
                         no_tray=True, port=0)
        try:
            self.assertTrue(app.start())
            self.assertEqual(app._window_size, (1100, 700))
            self.assertTrue(app.ui_state.set_window_size(1000, 650))
            self.assertEqual(app.ui_state.window_size(), (1000, 650))
            for _ in range(40):
                if app.runtime.state == "running":
                    break
                time.sleep(0.05)
            self.assertEqual(app.runtime.state, "running")
            self.assertTrue(app.pause())
            self.assertEqual(app.display_state(), "paused")
            self.assertTrue(app.resume())
            self.assertEqual(app.display_state(), "running")
            from desktop import platform_services
            closed = []
            original = platform_services.close_panel_window
            platform_services.close_panel_window = lambda hwnd: closed.append(hwnd)
            app._panel_hwnd = 12345
            try:
                app.shutdown()
            finally:
                platform_services.close_panel_window = original
            self.assertEqual(closed, [12345])
            text = Path(app.engine.LOG_FILE).read_text(encoding="utf-8")
            self.assertIn("桌面面板退出", text)
            self.assertFalse(os.path.exists(app.runtime_file))
        finally:
            app.shutdown()
            if old is None:
                os.environ.pop("ZRW_MUTEX_NAME", None)
            else:
                os.environ["ZRW_MUTEX_NAME"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
