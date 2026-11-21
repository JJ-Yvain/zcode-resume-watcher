# -*- coding: utf-8 -*-
"""macOS 菜单栏入口（rumps / PyObjC）。

Windows 用 ctypes 托盘；macOS 用系统菜单栏，避免用户面对 Terminal 和 .command。
rumps 只在 darwin 分支导入，Windows 源码运行不会因缺依赖而失败。
"""
import signal
import threading


def run_menu(desktop):
    """在 darwin 主线程跑菜单栏；返回 False 表示 rumps 不可用，调用方回落信号循环。"""
    try:
        import rumps
    except Exception:
        return False

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, lambda *_: desktop.request_quit())
            except (OSError, ValueError):
                pass

    class MacMenu(rumps.App):
        def __init__(self):
            super().__init__("ZCode 续命监视器", title="ZCode",
                             quit_button=None)
            self.desktop = desktop
            self.menu = [
                rumps.MenuItem("打开面板", callback=self.open_panel),
                rumps.MenuItem("暂停 / 继续", callback=self.toggle_pause),
                rumps.MenuItem("测试发送", callback=self.test_send),
                rumps.separator,
                rumps.MenuItem("打开原始日志", callback=self.open_log),
                rumps.MenuItem("打开配置", callback=self.open_config),
                rumps.separator,
                rumps.MenuItem("退出", callback=self.quit),
            ]
            self.timer = rumps.Timer(self.refresh, 1)
            self.timer.start()

        def refresh(self, _sender):
            state = self.desktop.display_state()
            self.title = {"running": "ZCode",
                          "paused": "ZCode ⏸",
                          "error": "ZCode ⚠"}.get(state, "ZCode")
            if self.desktop.quit_event.is_set():
                rumps.quit_application()

        def open_panel(self, _sender):
            self.desktop.open_panel()

        def toggle_pause(self, _sender):
            self.desktop.toggle_pause()

        def test_send(self, _sender):
            response = rumps.alert(
                "测试发送",
                "向当前 ZCode 会话投递一条测试消息？\n\n"
                "只确认文字已入队，不核验送达。",
                ok="发送", cancel="取消")
            if response == 1:
                threading.Thread(target=self.desktop.test_send,
                                 name="mac-test-send", daemon=True).start()

        def open_log(self, _sender):
            self.desktop.open_log()

        def open_config(self, _sender):
            self.desktop.open_config()

        def quit(self, _sender):
            self.desktop.request_quit()
            rumps.quit_application()

    MacMenu().run()
    return True
