# -*- coding: utf-8 -*-
"""Windows 托盘:ctypes 直连 shell32,不引入 pystray/Pillow。

菜单与状态刷新都在托盘线程内完成;测试发送等耗时动作由回调自行丢到后台线程,
避免菜单卡死。macOS 端 v1 不启用托盘,由 .command 前台进程 + SIGTERM 有序退出。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_APP = 0x8000
WM_NULL = 0x0000
WM_TRAY = WM_APP + 1

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIM_SETVERSION = 4
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_SHOWTIP = 0x80
NOTIFYICON_VERSION_4 = 4

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_GRAYED = 0x0001
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

ID_OPEN = 1001
ID_TOGGLE = 1002
ID_TEST = 1003
ID_LOG = 1004
ID_CONFIG = 1005
ID_QUIT = 1006

_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT,
                              wt.WPARAM, wt.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD),
                ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wt.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class TrayIcon:
    """一个托盘图标 + 隐藏消息窗口。run() 阻塞到退出。"""

    def __init__(self, callbacks, snapshot, assets_dir, title="ZCode 续命监视器"):
        if sys.platform != "win32":
            raise RuntimeError("tray_win only supports Windows")
        self.callbacks = callbacks
        self.snapshot = snapshot
        self.assets_dir = assets_dir
        self.title = title
        self._hwnd = None
        self._nid = None
        self._icons = {}
        self._current_icon = None
        self._current_state = None
        self._wndproc = None
        self._user32 = ctypes.windll.user32
        self._shell32 = ctypes.windll.shell32
        self._kernel32 = ctypes.windll.kernel32
        self._declare_prototypes()

    def _declare_prototypes(self):
        u, s, k = self._user32, self._shell32, self._kernel32
        u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                                 ctypes.c_int, ctypes.c_int, wt.UINT]
        u.LoadImageW.restype = wt.HANDLE
        u.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
        u.LoadIconW.restype = wt.HICON
        u.CreatePopupMenu.restype = wt.HMENU
        u.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t,
                                  wt.LPCWSTR]
        u.AppendMenuW.restype = wt.BOOL
        u.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wt.HWND,
                                     ctypes.c_void_p]
        u.TrackPopupMenu.restype = ctypes.c_int
        u.DestroyMenu.argtypes = [wt.HMENU]
        u.DestroyMenu.restype = wt.BOOL
        u.SetForegroundWindow.argtypes = [wt.HWND]
        u.SetForegroundWindow.restype = wt.BOOL
        u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.PostMessageW.restype = wt.BOOL
        u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        u.GetCursorPos.restype = wt.BOOL
        u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR,
                                      wt.DWORD, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wt.HWND,
                                      wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
        u.CreateWindowExW.restype = wt.HWND
        u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND,
                                  wt.UINT, wt.UINT]
        u.GetMessageW.restype = ctypes.c_int
        u.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        u.DispatchMessageW.restype = ctypes.c_ssize_t
        u.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
        u.SetTimer.restype = ctypes.c_size_t
        u.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
        u.DestroyWindow.argtypes = [wt.HWND]
        u.DestroyIcon.argtypes = [wt.HICON]
        s.Shell_NotifyIconW.argtypes = [wt.DWORD,
                                        ctypes.POINTER(NOTIFYICONDATAW)]
        s.Shell_NotifyIconW.restype = wt.BOOL
        k.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        k.GetModuleHandleW.restype = wt.HINSTANCE

    # ---- icon / tooltip ----
    def _icon_for(self, state):
        key = {"running": "run", "paused": "pause", "error": "error"}.get(
            state, "pause")
        handle = self._icons.get(key)
        if handle:
            return handle
        path = os.path.join(self.assets_dir, "tray-%s.ico" % key)
        if not os.path.exists(path):
            path = os.path.join(self.assets_dir, "tray-run.ico")
        handle = self._user32.LoadImageW(
            None, path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if not handle:
            handle = self._user32.LoadIconW(None, wt.LPARAM(32512))  # IDI_APPLICATION
        self._icons[key] = handle
        return handle

    def _tip(self, snap):
        state = snap.get("state", "starting")
        label = {"running": "运行中", "paused": "已暂停", "error": "引擎异常",
                 "stopped": "已停止", "starting": "启动中"}.get(state, state)
        return "%s · %s" % (self.title, label)

    def _apply(self):
        try:
            snap = self.snapshot() or {}
        except Exception:
            snap = {}
        state = snap.get("state", "starting")
        icon = self._icon_for(state)
        tip = self._tip(snap)
        nid = self._nid
        if nid is None:
            return
        nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE | NIF_SHOWTIP
        nid.hIcon = icon
        nid.szTip = tip[:127]
        self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        self._current_state = state
        self._current_icon = icon

    # ---- menu ----
    def _show_menu(self):
        menu = self._user32.CreatePopupMenu()
        if not menu:
            return
        snap = {}
        try:
            snap = self.snapshot() or {}
        except Exception:
            pass
        state = snap.get("state", "starting")
        paused = state == "paused"
        labels = {"running": "运行中", "paused": "已暂停", "error": "引擎异常",
                  "stopped": "已停止", "starting": "启动中"}
        status = ctypes.create_unicode_buffer(
            "状态：%s" % labels.get(state, state))
        self._user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, status)
        self._user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        self._user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "打开面板")
        toggle = "继续自动续命" if paused else "暂停自动续命"
        toggle_flags = MF_STRING | (MF_GRAYED if state in ("error", "stopped") else 0)
        self._user32.AppendMenuW(menu, toggle_flags, ID_TOGGLE, toggle)
        test_flags = MF_STRING | (MF_GRAYED if state in ("error", "stopped") else 0)
        self._user32.AppendMenuW(menu, test_flags, ID_TEST, "测试发送…")
        self._user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        self._user32.AppendMenuW(menu, MF_STRING, ID_LOG, "打开原始日志")
        self._user32.AppendMenuW(menu, MF_STRING, ID_CONFIG, "打开配置")
        self._user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        self._user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出")

        pt = wt.POINT()
        self._user32.GetCursorPos(ctypes.byref(pt))
        self._user32.SetForegroundWindow(self._hwnd)
        cmd = self._user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
            pt.x, pt.y, 0, self._hwnd, None)
        self._user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        self._user32.DestroyMenu(menu)
        if cmd:
            self._dispatch(cmd)

    def _dispatch(self, cmd):
        if cmd == ID_OPEN:
            self._call("open")
        elif cmd == ID_TOGGLE:
            self._call("toggle_pause")
        elif cmd == ID_TEST:
            self._call("test_send")
        elif cmd == ID_LOG:
            self._call("open_log")
        elif cmd == ID_CONFIG:
            self._call("open_config")
        elif cmd == ID_QUIT:
            self._call("quit")

    def _call(self, name):
        fn = self.callbacks.get(name)
        if fn is None:
            return
        try:
            fn()
        except Exception:
            pass

    # ---- window ----
    def _wndproc_impl(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            mouse = lparam & 0xFFFF
            if mouse in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._call("open")
            elif mouse in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
            return 0
        if msg == WM_COMMAND:
            self._dispatch(wparam & 0xFFFF)
            return 0
        if msg == WM_TIMER:
            self._apply()
            return 0
        if msg == WM_CLOSE:
            self._user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        """创建托盘并进入消息循环;返回 True 表示正常退出。"""
        hinst = self._kernel32.GetModuleHandleW(None)
        class_name = "ZCodeResumeWatcherTrayWindow"
        self._wndproc = _WNDPROC(self._wndproc_impl)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        self._user32.RegisterClassW(ctypes.byref(wc))
        self._hwnd = self._user32.CreateWindowExW(
            0, class_name, self.title, 0, 0, 0, 0, 0,
            None, None, hinst, None)
        if not self._hwnd:
            return False

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE | NIF_SHOWTIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icon_for("starting")
        nid.szTip = self._tip({"state": "starting"})[:127]
        self._nid = nid
        if not self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._user32.DestroyWindow(self._hwnd)
            return False
        nid.uVersion = NOTIFYICON_VERSION_4
        self._shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
        self._user32.SetTimer(self._hwnd, 1, 1000, None)
        self._apply()

        msg = wt.MSG()
        while True:
            ret = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

        self._user32.KillTimer(self._hwnd, 1)
        self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        for icon in self._icons.values():
            if icon:
                try:
                    self._user32.DestroyIcon(icon)
                except Exception:
                    pass
        self._icons.clear()
        self._hwnd = None
        return True

    def stop(self):
        if self._hwnd:
            try:
                self._user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
