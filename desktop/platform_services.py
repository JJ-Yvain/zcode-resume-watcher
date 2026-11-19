# -*- coding: utf-8 -*-
"""系统能力的唯一出口:Edge app 窗/打开文件/消息框/任务栏身份。

``web_app`` 不直接 import Windows API;``tray_win.py`` 也只在自身后端里
调用系统 API。这样 macOS 端可以复用同一套面板与状态模型。
"""
import os
import subprocess
import sys
import threading
import time

IS_WINDOWS = (sys.platform == "win32")
IS_MACOS = (sys.platform == "darwin")


def set_app_user_model_id():
    """让 Windows 任务栏把面板归到独立应用，而不是 pythonw。"""
    if not IS_WINDOWS:
        return False
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ZCodeResumeWatcher.Desktop")
        return True
    except Exception:
        return False


def _panel_windows(title):
    """枚举标题完全匹配的顶层窗口;用于给新开的 Edge app 窗定尺寸。"""
    handles = set()
    if not IS_WINDOWS:
        return handles
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows.argtypes = [proc, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int

    def cb(hwnd, _lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value != title:
            return True
        # 托盘的隐藏消息窗与面板窗同名;必须按 class 排除,否则会把托盘窗当面板"唤回",
        # 显示出一个空白/黑色的 1440×900 窗口(2026-11-19 实机反馈的根因之一)。
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "ZCodeResumeWatcherTrayWindow":
            handles.add(int(hwnd))
        return True

    user32.EnumWindows(proc(cb), 0)
    return handles


_APP_ICON_HANDLES = []


def _set_window_icon(hwnd):
    """给 Edge app 窗强制设置本应用图标;Edge 有时会用默认浏览器图标。"""
    if not IS_WINDOWS:
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "app.ico")
    if not os.path.exists(path):
        return
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                                  ctypes.c_int, ctypes.c_int, wt.UINT]
    user32.LoadImageW.restype = wt.HANDLE
    user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    for size, which in ((16, 0), (32, 1)):
        hicon = user32.LoadImageW(None, path, 1, size, size, 0x0010)
        if not hicon:
            continue
        _APP_ICON_HANDLES.append(hicon)
        try:
            user32.SendMessageW(hwnd, 0x0080, which, hicon)   # WM_SETICON
        except Exception:
            pass


def _fit_window(hwnd, width, height):
    """把新窗口恢复并居中到目标尺寸;屏幕更小时按工作区收缩。"""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wt.UINT]
    user32.SetWindowPos.restype = wt.BOOL
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.ShowWindow(hwnd, 9)                     # SW_RESTORE
    _set_window_icon(hwnd)
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    w = min(int(width), max(640, sw - 80))
    h = min(int(height), max(480, sh - 120))
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 3)
    user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0040)   # SWP_SHOWWINDOW
    user32.SetForegroundWindow(hwnd)


def close_panel_window(hwnd):
    """只关闭本应用记录的那一个面板窗;不按标题扫全部,避免误关别的实例/浏览器窗。"""
    if not IS_WINDOWS or not hwnd:
        return False
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostMessageW.restype = wt.BOOL
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsWindow.restype = wt.BOOL
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    try:
        #  minimized 状态下关闭,Edge 会把"最小化"记成上次状态,下次 --app 继续最小化。
        user32.ShowWindow(int(hwnd), 9)                 # SW_RESTORE
        time.sleep(0.15)
        user32.PostMessageW(int(hwnd), 0x0112, 0xF060, 0)   # SC_CLOSE
        for _ in range(20):
            if not user32.IsWindow(int(hwnd)):
                return True
            time.sleep(0.1)
        return True
    except Exception:
        return False


def window_size(hwnd):
    """返回窗口当前逻辑尺寸;最小化/最大化/异常尺寸返回 None。"""
    if not IS_WINDOWS or not hwnd:
        return None
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetWindowRect.restype = wt.BOOL
    user32.IsIconic.argtypes = [wt.HWND]
    user32.IsIconic.restype = wt.BOOL
    user32.IsZoomed.argtypes = [wt.HWND]
    user32.IsZoomed.restype = wt.BOOL
    try:
        if user32.IsIconic(int(hwnd)) or user32.IsZoomed(int(hwnd)):
            return None
        rect = wt.RECT()
        if not user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
            return None
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 320 or h < 240:
            return None
        return (int(w), int(h))
    except Exception:
        return None


def find_panel_window(title="ZCode 续命监视器"):
    """返回标题匹配的一个面板窗句柄(仅用于诊断/兼容,不用于批量关闭)。"""
    if not IS_WINDOWS:
        return None
    handles = _panel_windows(title)
    return int(sorted(handles)[-1]) if handles else None


def focus_panel_window(title="ZCode 续命监视器", width=1440, height=900):
    """已有面板窗口就恢复并置前;返回句柄,没有则 None。

    正常/最大化的窗口只唤回,不覆盖用户尺寸;最小化或掉到屏幕外(如 159×27)
    才按保存尺寸恢复,避免"托盘点了但窗口在 -25600 处看不见"。
    """
    if not IS_WINDOWS:
        return None
    handles = _panel_windows(title)
    if not handles:
        return None
    hwnd = int(sorted(handles)[-1])
    try:
        import ctypes
        import ctypes.wintypes as wt
        user32 = ctypes.windll.user32
        user32.IsIconic.argtypes = [wt.HWND]
        user32.IsIconic.restype = wt.BOOL
        user32.IsZoomed.argtypes = [wt.HWND]
        user32.IsZoomed.restype = wt.BOOL
        user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wt.HWND]
        if user32.IsZoomed(hwnd):
            user32.SetForegroundWindow(hwnd)
            return hwnd
        if window_size(hwnd) is None:      # 最小化 / 离屏占位 / 异常尺寸
            _fit_window(hwnd, width, height)
        else:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
        return hwnd
    except Exception:
        return None


def close_panel_windows(title="ZCode 续命监视器"):
    """关闭上一轮遗留的面板窗口(服务已死,留着只会显示断连/黑屏)。"""
    if not IS_WINDOWS:
        return 0
    handles = _panel_windows(title)
    if not handles:
        return 0
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostMessageW.restype = wt.BOOL
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    closed = 0
    for hwnd in handles:
        try:
            # 先恢复正常态再关,避免 Edge 把最小化状态记进 session restore。
            user32.ShowWindow(int(hwnd), 9)                 # SW_RESTORE
            time.sleep(0.15)
            # Edge 的顶层 app 窗忽略 WM_CLOSE;WM_SYSCOMMAND/SC_CLOSE 才会真的关。
            user32.PostMessageW(int(hwnd), 0x0112, 0xF060, 0)
            closed += 1
        except Exception:
            pass
    # 等窗口真的消失再开新窗;否则 Edge 会把新命令转发给正在关闭的旧窗。
    for _ in range(20):
        if not _panel_windows(title):
            break
        time.sleep(0.1)
    return closed


def _place_new_window(before, width, height, title="ZCode 续命监视器",
                      on_window=None):
    """后台等新窗口出现再定尺寸;不阻塞启动。"""
    for _ in range(120):
        time.sleep(0.25)
        handles = _panel_windows(title)
        fresh = handles - before
        # Edge 冷启动慢/复用窗口时可能没有"全新句柄";退化为修正当前唯一匹配窗。
        target = fresh if fresh else handles
        if target:
            for hwnd in target:
                try:
                    _fit_window(hwnd, width, height)
                except Exception:
                    pass
            if on_window is not None:
                for hwnd in target:
                    try:
                        on_window(int(hwnd))
                    except Exception:
                        pass
            # Edge 会在页面恢复后把保存的窗口尺寸再盖回来,补一次。
            time.sleep(1.2)
            for hwnd in _panel_windows(title) & target:
                try:
                    _fit_window(hwnd, width, height)
                except Exception:
                    pass
            return


def open_edge_app(url, width=1440, height=900, on_window=None):
    """用系统 Edge 的 --app 模式打开面板（无地址栏）；缺失时回落默认浏览器。"""
    if IS_WINDOWS:
        bases = [os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")]
        for base in bases:
            if not base:
                continue
            exe = os.path.join(base, "Microsoft", "Edge", "Application",
                               "msedge.exe")
            if not os.path.exists(exe):
                continue
            before = _panel_windows("ZCode 续命监视器")
            args = [exe,
                    "--window-size=%d,%d" % (int(width), int(height)),
                    "--no-first-run", "--no-default-browser-check",
                    "--app=" + url]
            try:
                subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError:
                continue
            threading.Thread(target=_place_new_window,
                             args=(before, width, height,
                                   "ZCode 续命监视器", on_window),
                             name="edge-window-fit", daemon=True).start()
            return True
    return open_path(url)


def open_path(path):
    """用系统默认程序打开文件/文件夹。返回是否成功启动。"""
    if not path:
        return False
    try:
        if IS_WINDOWS:
            os.startfile(path)
        elif IS_MACOS:
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def ask_yes_no(title, text):
    """原生是/否确认;非 Windows 返回 False(不擅自发送)。"""
    if IS_WINDOWS:
        import ctypes
        try:
            return ctypes.windll.user32.MessageBoxW(
                None, str(text), str(title), 0x04 | 0x20) == 6
        except Exception:
            pass
    return False


def message_box(title, text, error=False):
    """无控制台时的最后一道提示。Windows 用原生 MessageBox；其他平台写 stderr。"""
    if IS_WINDOWS:
        import ctypes
        try:
            ctypes.windll.user32.MessageBoxW(
                None, str(text), str(title), 0x10 if error else 0x40)
            return True
        except Exception:
            pass
    try:
        sys.stderr.write("[%s] %s\n" % (title, text))
    except Exception:
        pass
    return False
