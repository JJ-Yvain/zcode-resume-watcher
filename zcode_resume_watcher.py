#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZCode 断连自动续命监视器。

监听 ~/.zcode/cli/log/zcode-<本地日期>.jsonl 事件日志:turn 失败时自动
将 ZCode 主窗口置前、UIA 聚焦输入框、逐字符输入消息并回车发送。
触发判定分两种模式(config trigger_mode):
- whitelist(旧行为):仅连接重试耗尽(model_request_failed)与空响应
  (MODEL_ERROR+empty_model_response)触发;
- broad(默认):所有 turn.failed 默认触发——用户取消(TURN_CANCELLED 两种
  日志形态)一律排除,黑名单 exclude_error_causes/exclude_error_codes 再
  排除"重试无解"类;熔断器限制同会话连续自动发送次数(默认 5,
  出现 turn.completed 即复位),未知新错误类型因此默认被覆盖且有兜底。
  节奏由中断自身驱动:每次新中断(新 turnId)独立处理,不设时间冷却
  ——固定冷却会静默丢掉其他会话的独立中断(2026-09-11 实测移除,见复盘 P22)。
另尾随 rollout/model-io-sess_*.jsonl 模型 IO 日志,识别 provider 错误被
当成"假成功"正常回答返回的情况(不会有 turn.failed),三级判别:
B0 特征串全命中 → B1 结构特征(responseId 形态/错误信封文本,provider
无关) → B2 LLM 判官(可选,调 OpenAI 兼容端点二分类,fail-open)。
同一次报错只处理一次(requestId 去重);不同报错各发各的,统一由熔断封顶
(默认 5 次)——额度报错未必是真耗尽,中转路由抖动也返回同格式,多试常能恢复。
可选停滞看门狗(stall_enabled):turn.started 后长时间无任何事件且未
completed/failed,视为挂起,每个 turnId 只触发一次续命。
空闲心跳(heartbeat_minutes,默认 30 分钟)定期写一行"心跳"日志,让
`--audit` 覆盖审计能精确判定"监视器当时是否在运行"。
发送前默认启用「智能归位」:以 sessionId 反查本地库会话标题,在侧栏
定位并点击对应对话行(顶栏标题验证切换成功),再聚焦输入框;输入框有
未发送草稿时不发送(防拼接污染)。归位失败时按配置宁可不发。
零第三方依赖:纯标准库;检测/守卫/审计层跨平台。注入层按平台分发——
Windows: UIA 聚焦走同目录 uia_focus_input.ps1,智能归位走 uia_navigate_input.ps1,
注入链 置前(AttachThreadInput)→ UIA SetFocus → PostMessage WM_CHAR 逐字符 → 回车,
全程不依赖 SendInput(普通进程 SendInput 不可用);
macOS(v1): osascript 激活 + 剪贴板粘贴(Cmd+V) + 回车,无智能归位/草稿检测,
需在系统设置授予辅助功能权限。
另提供 `--audit` 覆盖审计(只读):扫描近 N 天事件日志/watcher.log/rollout,
输出①全部失败签名与现行处置(含"被黑名单排除、可调整"提示)②规则↔实际
动作交叉核验(找出"应触发但未处理"=漏发,并区分"未运行"与"运行中异常")
③悬空 turn 候选 ④rollout 错误报文候选(B0/B1 未覆盖的形状,供扩面)。"""
import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.request

# 平台判定:Windows 走 UIA+PostMessage,macOS 走 osascript(见 inject 分发);
# 判定/守卫/审计逻辑纯标准库,天然跨平台。
IS_WINDOWS = (sys.platform == "win32")
IS_MACOS = (sys.platform == "darwin")
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

# ------------------------- 版本与更新源 -------------------------
VERSION = "1.0.0"                     # 语义化版本,与 GitHub Releases tag(v前缀)比对
UPSTREAM_REPO = "JJ-Yvain/zcode-resume-watcher"   # --check-updates 查询的发布源
CHECK_UPDATES = True                  # 启动时查询最新 release;仅日志提示,绝不自动更新
MAC_APP_NAME = "ZCode"                # macOS 注入时激活的应用名(config: mac_app_name)

# ------------------------- 可调配置(默认值,可被 config.json 覆盖) -------------------------
MESSAGE = "任务意外中断，请继续！"          # 发送的续命文本
POLL_SECONDS = 1.0                          # 日志轮询间隔
# 说明: 原 cooldown_seconds(固定冷却)已于 2026-09-11 移除——它按"距离上次发送的秒数"放行,
# 与"是否真的又中断了一次"无关,会静默丢掉其他会话的独立中断(实测证据见复盘 P22)。
# 防刷屏由事件级守卫承担: 同 turn/requestId 去重 + 每会话熔断(5 次);不做按窗口去重
LOG_DIR = os.path.expanduser("~/.zcode/cli/log")
EXCLUDE_SESSION_PREFIXES = ("sess_subagent",)   # subagent 失败不打扰主对话
RESUME_ERROR_CODES = {"model_request_failed"}   # 连接/传输/5xx 重试耗尽
RESUME_EMPTY_REASON = "empty_model_response"    # 空响应失败原因
WINDOW_EXE = "zcode.exe"
WINDOW_TITLE = "ZCode"
# ---- provider 额度"假成功"检测(rollout 模型 IO 日志) ----
ROLLOUT_DIR = os.path.expanduser("~/.zcode/cli/rollout")
QUOTA_ENABLED = True                                   # 关闭则完全不读 rollout 目录
QUOTA_NEEDLES = ("qoder error 403", '"code":"115"', "agentLimitResetTime")
QUOTA_RESET_REGEX = r"agentLimitResetTime[^\d]{0,4}(\d{10,13})"
# ---- 智能归位(sessionId→会话标题→侧栏行 Invoke→验证后再发送) ----
NAVIGATE_ENABLED = True
DB_PATH = os.path.expanduser("~/.zcode/cli/db/db.sqlite")
NAV_ITEM_CLASS_REGEX = "task-item"                     # 侧栏对话行的 UIA ClassName 特征
NAV_GROUP_CLASS_REGEX = "space-y-2"                    # 项目分组头行特征(用于同名标题消歧)
NAV_HEADER_CLASS_REGEX = "min-w-12"                    # 顶栏"当前会话标题"Text 行的 ClassName 特征
NAV_FAIL_FALLBACK_SEND = False                         # 归位失败时:false=宁可不发 true=发给当前对话
DRAFT_CHECK_ENABLED = True                             # 发送前检测输入框草稿,非空则放弃(防拼接污染)
# ---- 全量接管(broad 触发模式)与熔断 ----
TRIGGER_MODE = "broad"          # broad=所有 turn.failed 默认触发; whitelist=仅白名单错误码(旧行为)
EXCLUDE_ERROR_CAUSES = (        # broad 模式按 cause.code 排除:用户取消/重试无解类
    "model_request_cancelled",
    "provider_not_configured", "MEDIA_BUDGET_CURRENT_IMAGE_TOO_LARGE")
EXCLUDE_ERROR_CODES = ("TURN_CANCELLED",)  # broad 模式按 error.code 排除(取消事件的另一种形态)
CIRCUIT_BREAKER_LIMIT = 5       # 同会话连续自动发送上限,出现 turn.completed 即复位
                                # (2026-09-11 用户决定: 从 2 提到 5——限流风暴希望多试几次;
                                #  model_rate_limited 同时移出黑名单,见 README 已知限制)
# ---- 假成功 B1 结构判别 / B2 LLM 判官 ----
QUOTA_STRUCTURAL_ENABLED = True
QUOTA_RESPONSEID_REGEX = r"(?i)-error"                         # responseId 形态(如 qoder-error-*)
QUOTA_TEXT_REGEX = r"^\s*\[?[a-z][a-z0-9_-]* error \d{3}[:\]]"  # 错误信封文本(如 "[qoder error 403:")
JUDGE_ENABLED = False
JUDGE_ENDPOINT = "http://127.0.0.1:20128/v1/chat/completions"
JUDGE_MODEL = ""                 # 空=判官不生效;judge_enabled 时必须配置模型名
JUDGE_COOLDOWN_SECONDS = 300.0   # 两次判官调用的最小间隔(全局限频)
JUDGE_MAX_CHARS = 400            # 送判官的文本截断长度
# ---- 停滞看门狗 ----
STALL_ENABLED = False
STALL_TIMEOUT_SECONDS = 900.0    # turn.started 后无任何事件超过该秒数视为停滞
# ---- 覆盖审计(--audit)与空闲心跳 ----
HEARTBEAT_MINUTES = 30           # 空闲心跳写日志间隔(分钟),0=关闭;审计据此精确判定运行窗口
AUDIT_DEFAULT_DAYS = 7           # --audit 默认扫描天数
AUDIT_WINDOW_BEFORE = 60         # 事件→watcher.log 动作匹配窗口(秒,向前)
AUDIT_WINDOW_AFTER = 300         # 同上(向后)
NAV_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "navigate.log")
__CONFIG_LOADED = False  # 防止重复加载日志


def load_config(path=None):
    """读取 config.json(缺省为脚本同目录),覆盖模块级常量。配置缺省时保持默认值。"""
    global __CONFIG_LOADED, MESSAGE, POLL_SECONDS, LOG_DIR
    global EXCLUDE_SESSION_PREFIXES, RESUME_ERROR_CODES, RESUME_EMPTY_REASON
    global WINDOW_EXE, WINDOW_TITLE
    global ROLLOUT_DIR, QUOTA_ENABLED, QUOTA_NEEDLES, QUOTA_RESET_REGEX
    global NAVIGATE_ENABLED, DB_PATH, NAV_ITEM_CLASS_REGEX, NAV_GROUP_CLASS_REGEX
    global NAV_HEADER_CLASS_REGEX, NAV_FAIL_FALLBACK_SEND, DRAFT_CHECK_ENABLED
    global TRIGGER_MODE, EXCLUDE_ERROR_CAUSES, EXCLUDE_ERROR_CODES, CIRCUIT_BREAKER_LIMIT
    global QUOTA_STRUCTURAL_ENABLED, QUOTA_RESPONSEID_REGEX, QUOTA_TEXT_REGEX
    global JUDGE_ENABLED, JUDGE_ENDPOINT, JUDGE_MODEL, JUDGE_COOLDOWN_SECONDS, JUDGE_MAX_CHARS
    global STALL_ENABLED, STALL_TIMEOUT_SECONDS, HEARTBEAT_MINUTES
    global CHECK_UPDATES, MAC_APP_NAME
    cfg_path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.exists(cfg_path):
        return
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log("config.json 加载失败,使用默认值: %s" % e)
        return
    MESSAGE = cfg.get("message", MESSAGE)
    POLL_SECONDS = cfg.get("poll_seconds", POLL_SECONDS)
    LOG_DIR = os.path.expanduser(cfg.get("log_dir", "~/.zcode/cli/log"))
    EXCLUDE_SESSION_PREFIXES = tuple(cfg.get("exclude_session_prefixes", list(EXCLUDE_SESSION_PREFIXES)))
    RESUME_ERROR_CODES = set(cfg.get("resume_error_codes", list(RESUME_ERROR_CODES)))
    RESUME_EMPTY_REASON = cfg.get("resume_empty_reason", RESUME_EMPTY_REASON)
    WINDOW_EXE = cfg.get("window_exe", WINDOW_EXE)
    WINDOW_TITLE = cfg.get("window_title", WINDOW_TITLE)
    ROLLOUT_DIR = os.path.expanduser(cfg.get("rollout_dir", "~/.zcode/cli/rollout"))
    QUOTA_ENABLED = bool(cfg.get("quota_enabled", QUOTA_ENABLED))
    QUOTA_NEEDLES = tuple(cfg.get("quota_needles", list(QUOTA_NEEDLES)))
    QUOTA_RESET_REGEX = _compile_or_default(cfg.get("quota_reset_regex", QUOTA_RESET_REGEX),
                                            QUOTA_RESET_REGEX, "quota_reset_regex")
    NAVIGATE_ENABLED = bool(cfg.get("navigate_enabled", NAVIGATE_ENABLED))
    DB_PATH = os.path.expanduser(cfg.get("db_path", "~/.zcode/cli/db/db.sqlite"))
    NAV_ITEM_CLASS_REGEX = _compile_or_default(cfg.get("nav_item_class_regex", NAV_ITEM_CLASS_REGEX),
                                               NAV_ITEM_CLASS_REGEX, "nav_item_class_regex")
    NAV_GROUP_CLASS_REGEX = _compile_or_default(cfg.get("nav_group_class_regex", NAV_GROUP_CLASS_REGEX),
                                                NAV_GROUP_CLASS_REGEX, "nav_group_class_regex")
    NAV_HEADER_CLASS_REGEX = _compile_or_default(cfg.get("nav_header_class_regex", NAV_HEADER_CLASS_REGEX),
                                                 NAV_HEADER_CLASS_REGEX, "nav_header_class_regex")
    NAV_FAIL_FALLBACK_SEND = bool(cfg.get("navigate_fail_fallback_send", NAV_FAIL_FALLBACK_SEND))
    DRAFT_CHECK_ENABLED = bool(cfg.get("draft_check_enabled", DRAFT_CHECK_ENABLED))
    TRIGGER_MODE = cfg.get("trigger_mode", TRIGGER_MODE)
    EXCLUDE_ERROR_CAUSES = tuple(cfg.get("exclude_error_causes", list(EXCLUDE_ERROR_CAUSES)))
    EXCLUDE_ERROR_CODES = tuple(cfg.get("exclude_error_codes", list(EXCLUDE_ERROR_CODES)))
    try:
        CIRCUIT_BREAKER_LIMIT = max(1, int(cfg.get("circuit_breaker_limit", CIRCUIT_BREAKER_LIMIT)))
    except (TypeError, ValueError):
        log("配置 circuit_breaker_limit 不是整数,回退默认 %d" % CIRCUIT_BREAKER_LIMIT)
    QUOTA_STRUCTURAL_ENABLED = bool(cfg.get("quota_structural_enabled", QUOTA_STRUCTURAL_ENABLED))
    QUOTA_RESPONSEID_REGEX = _compile_or_default(
        cfg.get("quota_responseid_regex", QUOTA_RESPONSEID_REGEX),
        QUOTA_RESPONSEID_REGEX, "quota_responseid_regex")
    QUOTA_TEXT_REGEX = _compile_or_default(
        cfg.get("quota_text_regex", QUOTA_TEXT_REGEX), QUOTA_TEXT_REGEX, "quota_text_regex")
    JUDGE_ENABLED = bool(cfg.get("judge_enabled", JUDGE_ENABLED))
    JUDGE_ENDPOINT = cfg.get("judge_endpoint", JUDGE_ENDPOINT)
    JUDGE_MODEL = cfg.get("judge_model", JUDGE_MODEL)
    JUDGE_COOLDOWN_SECONDS = float(cfg.get("judge_cooldown_seconds", JUDGE_COOLDOWN_SECONDS))
    JUDGE_MAX_CHARS = int(cfg.get("judge_max_chars", JUDGE_MAX_CHARS))
    STALL_ENABLED = bool(cfg.get("stall_enabled", STALL_ENABLED))
    STALL_TIMEOUT_SECONDS = float(cfg.get("stall_timeout_seconds", STALL_TIMEOUT_SECONDS))
    try:
        HEARTBEAT_MINUTES = max(0, int(cfg.get("heartbeat_minutes", HEARTBEAT_MINUTES)))
    except (TypeError, ValueError):
        log("配置 heartbeat_minutes 不是整数,回退默认 %d" % HEARTBEAT_MINUTES)
    CHECK_UPDATES = bool(cfg.get("check_updates", CHECK_UPDATES))
    MAC_APP_NAME = cfg.get("mac_app_name", MAC_APP_NAME)
    if not __CONFIG_LOADED:
        log("config.json 已加载: %s" % cfg_path)
        __CONFIG_LOADED = True

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watcher.log")

# ------------------------- win32 基础(仅 Windows 加载;macOS 走 osascript) -------------------------
if IS_WINDOWS:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


def log(msg):
    line = "%s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _ver_tuple(s):
    try:
        return tuple(int(x) for x in s.lstrip("vV").split("."))
    except ValueError:
        return None


def check_updates():
    """查询 GitHub 最新 release 并与 VERSION 比对。只提示不自动下载;
    网络不通/未发布 release 时静默降级为一行日志,绝不影响监视主循环。"""
    url = "https://api.github.com/repos/%s/releases/latest" % UPSTREAM_REPO
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "zcode-resume-watcher/%s" % VERSION})
        with urllib.request.urlopen(req, timeout=5) as r:
            tag = (json.load(r) or {}).get("tag_name") or ""
    except Exception as e:
        log("检查更新跳过(不影响监视): %s" % e)
        return
    cur, new = _ver_tuple(VERSION), _ver_tuple(tag)
    if cur and new and new > cur:
        log("发现新版本 %s(当前 v%s):https://github.com/%s/releases 可获取"
            % (tag, VERSION, UPSTREAM_REPO))
    else:
        log("当前已是最新版本 v%s" % VERSION)


def _exe_name_for_pid(pid):
    if not pid:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def find_zcode_hwnd():
    """返回 ZCode 主窗口句柄:优先标题为 ZCode 的可见窗口中面积最大的
    (ZCode 有多个同名窗口,如悬浮小窗,故不能只认第一个)。"""
    cands = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _exe_name_for_pid(pid.value).lower() != WINDOW_EXE:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, title, n + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        cands.append((hwnd, title.value, area))
        return True

    user32.EnumWindows(_cb, 0)
    if not cands:
        return None
    titled = [c for c in cands if c[1] == WINDOW_TITLE]
    pool = titled or cands
    return max(pool, key=lambda c: c[2])[0]


def bring_to_front(hwnd):
    """置前并还原最小化窗口;AttachThreadInput 绕过前台锁定限制。"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    my_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else None
    attached = []
    for t in (target_thread, fg_thread):
        if t and t != my_thread:
            if user32.AttachThreadInput(my_thread, t, True):
                attached.append(t)
    ok = bool(user32.SetForegroundWindow(hwnd))
    user32.BringWindowToTop(hwnd)
    for t in attached:
        user32.AttachThreadInput(my_thread, t, False)
    return ok


# ------------------------- 按键常量 -------------------------
VK_RETURN = 0x0D
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102


def _focused_child_hwnd(hwnd):
    """返回 hwnd 所属线程的焦点控件;取不到则返回 hwnd 本身。"""
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    if not tid:
        return hwnd
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(_GUITHREADINFO)
    if user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
        return info.hwndFocus
    return hwnd


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


def uia_focus_input():
    """用 UIA 把聊天输入框设为页面焦点(ZCode 输入框是 React 内容化编辑,
    页面无权焦时任何键盘注入都无效;UIA SetFocus 不依赖 SendInput)。
    重试 3 次:Chromium 无障碍树懒初始化,应用刚启动后的首次查询可能返回空。"""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uia_focus_input.ps1")
    proc_name = os.path.splitext(WINDOW_EXE)[0]
    for attempt in range(3):
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                "-File", script, "-ProcName", proc_name],
                               capture_output=True, timeout=15)
            if r.returncode == 0:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ------------------------- 智能归位 -------------------------
NAV_VERDICTS = {1: "窗口不可用", 2: "侧栏无唯一匹配行(未找到或歧义)", 3: "已点击但顶栏标题未在3秒内变为目标",
                4: "界面树未就绪", 5: "已切换但无输入框", 6: "输入框有未发送草稿,为不污染原对话放弃发送",
                10: "导航参数错误", -1: "导航脚本执行异常"}


def _compile_or_default(pattern, default, name):
    """配置里的正则非法时回退默认值并告警(ps1 端 -match 遇非法正则会直接崩溃,必须前置校验)。"""
    try:
        re.compile(pattern)
        return pattern
    except re.error:
        log("配置 %s 不是合法正则(%r),回退默认 %r" % (name, pattern, default))
        return default


def _encode_cp16(s):
    """把字符串编码为 UTF-16 码点单元十进制串(纯 ASCII 传参,规避 ps1/bat 编码坑,
    并正确处理 emoji 等增补平面字符的代理对)。"""
    b = s.encode("utf-16-le")
    return ",".join(str(u) for u in struct.unpack("<%dH" % (len(b) // 2), b)) if b else ""


def _db_conn():
    if not os.path.exists(DB_PATH):
        return None
    try:
        return sqlite3.connect("file:" + DB_PATH.replace(os.sep, "/") + "?mode=ro",
                               uri=True, timeout=3)
    except sqlite3.Error:
        return None


def resolve_session_target(sess):
    """sessionId → {title, project, time_updated};库缺失/无记录/标题为空返回 None。"""
    if not sess:
        return None
    con = _db_conn()
    if con is None:
        return None
    try:
        row = con.execute("SELECT title, directory, time_updated FROM session WHERE id=?",
                          (sess,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row or not row[0]:
        return None
    directory = row[1] or ""
    return {"title": row[0], "project": os.path.basename(directory.rstrip("\\/")),
            "time_updated": row[2]}


def run_nav_script(target, dry=False):
    """执行 uia_navigate_input.ps1,返回其退出码(见 NAV_VERDICTS)。"""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uia_navigate_input.ps1")
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
            "-ProcName", os.path.splitext(WINDOW_EXE)[0],
            "-TitleCP", _encode_cp16(target["title"]),
            "-ProjectCP", _encode_cp16(target.get("project") or ""),
            "-ItemClassRegex", NAV_ITEM_CLASS_REGEX,
            "-GroupClassRegex", NAV_GROUP_CLASS_REGEX,
            "-HeaderClassRegex", NAV_HEADER_CLASS_REGEX,
            "-Log", NAV_LOG]
    if dry:
        args.append("-DryRun")
    elif DRAFT_CHECK_ENABLED:
        args.append("-DraftCheck")
    try:
        r = subprocess.run(args, capture_output=True, timeout=30)
        return r.returncode
    except Exception as e:
        log("导航脚本执行异常: %s" % e)
        return -1


def navigate_to_session(hwnd, target, sess):
    """把 ZCode 界面切到出错会话并验证。返回 (归位成功?, 脚本是否已聚焦输入框?)。
    验证 oracle:顶栏标题 Text 变为目标 title(逐字相等,无时间后缀);若打开时顶栏已是
    目标标题则视为"已在场",免点击直接聚焦。"""
    if target is None:
        log("归位失败: DB 解析不到会话 %s 的标题" % sess)
        return False, False
    if not bring_to_front(hwnd):
        return False, False
    time.sleep(0.4)
    code = run_nav_script(target, dry=False)
    for _ in range(2):  # 界面树未就绪/窗口瞬时不可用:短暂重试(Chromium 懒建树;重试前重新置前)
        if code not in (1, 4):
            break
        time.sleep(0.6)
        bring_to_front(hwnd)
        code = run_nav_script(target, dry=False)
    if code == 0:
        log("归位成功(顶栏标题验证): 「%s」" % target["title"])
        return True, True
    if code == 5:
        log("归位成功但脚本未见输入框,走普通聚焦: 「%s」" % target["title"])
        return True, False
    log("归位失败: %s 「%s」(详见 navigate.log)" % (NAV_VERDICTS.get(code, "退出码 %s" % code),
                                                    target["title"]))
    return False, False


def inject_text(hwnd, text, focus_done=False):
    """置前 → UIA 聚焦输入框 → PostMessage 逐字符 WM_CHAR → 回车发送。
    focus_done=True 表示归位脚本已把页面焦点放到输入框,跳过普通聚焦步骤。
    全程不依赖 SendInput(本机 SendInput 在普通进程不可用,见 README)。"""
    if not bring_to_front(hwnd):
        log("注入失败: 窗口置前失败")
        return False
    time.sleep(0.4)
    if not focus_done:
        uia_focus_input()
        time.sleep(0.3)
    target = _focused_child_hwnd(hwnd)
    if not target:
        log("注入失败: 找不到焦点控件")
        return False
    for ch in text:
        if not user32.PostMessageW(target, WM_CHAR, ord(ch), 1):
            log("注入失败: WM_CHAR 投递失败于字符 %r" % ch)
            return False
        time.sleep(0.02)
    time.sleep(0.3)
    if not user32.PostMessageW(target, WM_KEYDOWN, VK_RETURN, 0x001C0001):
        log("注入失败: 回车按下投递失败")
        return False
    if not user32.PostMessageW(target, WM_KEYUP, VK_RETURN, 0xC01C0001):
        log("注入失败: 回车抬起投递失败")
        return False
    return True


# ------------------------- macOS 注入(osascript) -------------------------
def mac_inject_text(text):
    """macOS 注入:激活 ZCode → 文本经 argv 进 AppleScript 剪贴板 → Cmd+V → 回车 →
    恢复原剪贴板。中文/emoji 走剪贴板粘贴(System Events keystroke 对 CJK 不可靠);
    文本经 osascript 的 run argv 传入,无需手工转义。
    前置条件:在 系统设置 → 隐私与安全性 → 辅助功能 中授权运行环境(终端/Python)。
    v1 限制:不做智能归位与草稿检测(AX 树待实测),发送目标为 ZCode 当前焦点会话。"""
    old_clip = ""
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, timeout=5)
        old_clip = r.stdout.decode("utf-8", "replace")
    except Exception:
        pass  # 读不回旧剪贴板就放弃恢复,不影响发送
    script = (
        'on run argv\n'
        'tell application "%s" to activate\n' % MAC_APP_NAME +
        'delay 0.6\n'
        'tell application "System Events"\n'
        'set the clipboard to (item 1 of argv)\n'
        'keystroke "v" using command down\n'
        'delay 0.4\n'
        'key code 36\n'
        'delay 0.3\n'
        'set the clipboard to (item 2 of argv)\n'
        'end tell\n'
        'end run'
    )
    try:
        r = subprocess.run(["osascript", "-e", script, text, old_clip],
                           capture_output=True, timeout=20)
    except OSError as e:
        log("注入失败: osascript 不可用: %s" % e)
        return False
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()
        log("注入失败: osascript 报错: %s" % err[:200])
        if "assist" in err.lower() or "not allowed" in err.lower() or "-25211" in err:
            log("提示: 需要在 系统设置 → 隐私与安全性 → 辅助功能 中授权运行环境后重试")
        return False
    return True


def _trigger_resume_macos(reason, evt, args):
    """macOS 触发链:无窗口句柄/智能归位/草稿检测(均依赖 Windows UIA),
    检测与守卫(去重/熔断)与 Windows 完全同一套,仅发送末端不同。"""
    sess = evt.get("sessionId") or ""
    if args.dry_run:
        log("[dry-run] 将发送 '%s' (%s, %s, 平台: macOS 无归位)" % (MESSAGE, reason, sess))
        return False
    sent = mac_inject_text(MESSAGE)
    if sent:
        log("已自动发送 '%s' (%s, %s)" % (MESSAGE, reason, sess))
    else:
        log("发送失败: 注入未被接受 (%s, %s)" % (reason, sess))
    return sent


# ------------------------- 日志判定 -------------------------
def classify_failure(evt):
    """turn 失败事件是否应触发续命;是则返回原因标签,否则 None。
    用户取消(两种日志形态)任何模式都排除;broad 模式下其余失败默认触发,
    黑名单(cause.code / error.code)排除"重试无解/风暴噪音"类。"""
    if evt.get("event") != "turn.failed":
        return None
    sess = evt.get("sessionId") or ""
    if any(sess.startswith(p) for p in EXCLUDE_SESSION_PREFIXES):
        return None
    err = evt.get("error") or {}
    cause = err.get("cause") or {}
    cause_code = cause.get("code")
    err_code = err.get("code")
    if cause_code == "model_request_cancelled" or err_code == "TURN_CANCELLED":
        return None
    if cause_code in RESUME_ERROR_CODES:
        return "connection"
    if err_code == "MODEL_ERROR" and (err.get("context") or {}).get("reason") == RESUME_EMPTY_REASON:
        return "empty_response"
    if TRIGGER_MODE != "broad":
        return None
    if cause_code in EXCLUDE_ERROR_CAUSES or err_code in EXCLUDE_ERROR_CODES:
        return None
    return cause_code or "turn_failed"


def _event_dedup_key(evt):
    """事件的去重键:优先 turnId;缺失时用 (sessionId|timestamp|cause) 哈希兜底,
    保证"同一条日志行"永远不会被处理两次,而不误伤"同一会话的两次独立中断"。"""
    tid = evt.get("turnId")
    if tid:
        return tid
    err = evt.get("error") or {}
    raw = "%s|%s|%s" % (evt.get("sessionId") or "", evt.get("timestamp") or "",
                        (err.get("cause") or {}).get("code") or err.get("code") or "")
    return "h" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def describe_skip(evt):
    err = evt.get("error") or {}
    cause = err.get("cause") or {}
    sess = evt.get("sessionId") or ""
    if any(sess.startswith(p) for p in EXCLUDE_SESSION_PREFIXES):
        return "no trigger (subagent session)"
    if cause.get("code") == "model_request_cancelled":
        return "no trigger (user cancelled)"
    return "no trigger (cause=%s, error.code=%s)" % (cause.get("code"), err.get("code"))


# ------------------------- provider 额度"假成功"判定(rollout model-io) -------------------------
def fmt_reset(ms):
    """把额度重置毫秒时间戳转成可读串(本地时区);容忍秒级时间戳。"""
    if ms is None:
        return "未知"
    if ms < 10 ** 11:  # 10 位视为秒
        ms *= 1000
    try:
        return datetime.datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "非法值(%d)" % ms


def _extract_reset(text):
    """从错误文本提取额度重置毫秒时间戳;解析不到返回 None。"""
    try:
        m = re.search(QUOTA_RESET_REGEX, text)
        if m:
            return int(m.group(1))
    except re.error:
        log("quota_reset_regex 配置有误,按无重置时间处理: %s" % QUOTA_RESET_REGEX)
    return None


def _looks_like_error(resp, text):
    """结构门:responseId 是错误形态(如 qoder-error-*)或正文以错误信封开头。
    2026-09-12 实测(P25):上下文压缩摘要会原样引用 QUOTA_NEEDLES 特征串
    (对话讨论过额度功能),特征串命中必须叠加结构判别,否则
    "讨论错误的内容"会被当成"错误本身"。"""
    rid = resp.get("responseId")
    if isinstance(rid, str) and re.search(QUOTA_RESPONSEID_REGEX, rid):
        return True
    return bool(re.search(QUOTA_TEXT_REGEX, text))


def find_quota_hit(rec):
    """B0:model-io 记录的 response.text 若包含全部 QUOTA_NEEDLES 且过结构门
    → (reset_ms, text)。只查 response.text:request.body.messages 里有历史
    报错回显,查整行会误报。结构门见 _looks_like_error。"""
    if not isinstance(rec, dict):
        return None
    resp = rec.get("response")
    if not isinstance(resp, dict):
        return None
    text = resp.get("text")
    if not isinstance(text, str):
        return None
    for needle in QUOTA_NEEDLES:
        if needle not in text:
            return None
    if not _looks_like_error(resp, text):
        return None
    return _extract_reset(text), text


def find_structural_hit(rec):
    """B1:provider 无关的结构判别——responseId 形态(如 qoder-error-*)或
    错误信封文本(如 "[qoder error 403:")命中即疑似错误报文。
    空文本是重试链噪声(attempt 空记录实测),绝不判错。"""
    if not isinstance(rec, dict):
        return None
    resp = rec.get("response")
    if not isinstance(resp, dict):
        return None
    text = resp.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if not _looks_like_error(resp, text):
        return None
    return _extract_reset(text), text


# 宽泛错误词预筛:B2 判官只看"提到错误"的文本,避免对全部回答调接口。
LOOSE_ERRISH = re.compile(
    r"error|错误|失败|额度|限制|异常|超时|exceeded|limit|unavailable|timeout", re.I)


def classify_modelio(rec, allow_judge=True):
    """model-io 记录三级判别,命中返回 (label, reset_ms, text),否则 None。
    B0 特征串全命中+结构门 → B1 结构特征 → B2 LLM 判官(仅"提到错误"且未确认的文本)。
    结构正则只能保证"长得像错误报文",区分"报错本身"与"讨论错误的正常回答"
    需要语义判别(实测裸正则会把后者误报),故 B2 默认可开、fail-open。"""
    hit = find_quota_hit(rec)
    if hit:
        return ("额度报错",) + hit
    if not isinstance(rec, dict):
        return None
    resp = rec.get("response")
    if not isinstance(resp, dict):
        return None
    text = resp.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if QUOTA_STRUCTURAL_ENABLED:
        hit = find_structural_hit(rec)
        if hit:
            return ("错误报文(B1 结构特征)",) + hit
    if allow_judge and JUDGE_ENABLED and JUDGE_MODEL and LOOSE_ERRISH.search(text):
        if judge_text(text[:JUDGE_MAX_CHARS]) == "error":
            return ("错误报文(LLM 判官)", _extract_reset(text), text)
    return None


_judge_cache = {}
_judge_last_call = 0.0


def judge_text(text):
    """B2 LLM 判官:错误报文返回 "error",正常回答 "ok";限频/不可用返回 "skip"
    (fail-open=不触发发送)。按文本哈希缓存,全局限频防滥用。"""
    global _judge_last_call
    key = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
    if key in _judge_cache:
        return _judge_cache[key]
    now = time.monotonic()
    if now - _judge_last_call < JUDGE_COOLDOWN_SECONDS:
        return "skip"
    _judge_last_call = now
    payload = json.dumps({
        "model": JUDGE_MODEL, "temperature": 0, "max_tokens": 8,
        "messages": [
            {"role": "system", "content":
             "你是日志分类器。判断用户给出的文本是「模型/网关返回的错误报文」还是"
             "「正常的助手回答」。错误报文指接口报错、额度/限流、超时、服务不可用等"
             "错误信息本身;正常回答即使讨论到错误也算正常回答。"
             "只回答 ERROR 或 OK,不要任何其他内容。"},
            {"role": "user", "content": text[:JUDGE_MAX_CHARS]}]}).encode("utf-8")
    req = urllib.request.Request(JUDGE_ENDPOINT, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        content = str((((data or {}).get("choices") or [{}])[0].get("message") or {})
                      .get("content") or "").strip().upper()
        verdict = "error" if content.startswith("ERROR") else (
            "ok" if content.startswith("OK") else "skip")
    except Exception as e:
        log("LLM 判官调用失败,按正常回答处理(fail-open): %s" % e)
        verdict = "skip"
    _judge_cache[key] = verdict
    return verdict


def scan_rollout_files():
    """列出 rollout 下 (文件路径, sessionId);文件名形如 model-io-sess_<id>.jsonl,
    subagent 等排除前缀按文件名过滤。"""
    out = []
    try:
        names = os.listdir(ROLLOUT_DIR)
    except OSError:
        return out
    for name in names:
        if not (name.startswith("model-io-") and name.endswith(".jsonl")):
            continue
        sess = name[len("model-io-"):-len(".jsonl")]
        if any(sess.startswith(p) for p in EXCLUDE_SESSION_PREFIXES):
            continue
        out.append((os.path.join(ROLLOUT_DIR, name), sess))
    return out


# ------------------------- 触发状态 -------------------------
class Seen(object):
    """去重键集合(与旧实现同语义:集合随去重量增长,量级为会话内失败次数,可忽略)。"""

    def __init__(self, maxlen=2000):
        self.dq = collections.deque(maxlen=maxlen)
        self.s = set()

    def add_if_new(self, key):
        if key in self.s:
            return False
        self.s.add(key)
        self.dq.append(key)
        return True


class WatchState(object):
    def __init__(self):
        self.turns = Seen(2000)      # 事件日志 turnId
        self.requests = Seen(2000)   # model-io requestId
        self.sends_since_success = {}   # 会话 → 自上次 turn.completed 起的连续自动发送数(熔断)
        self.open_turns = {}            # 会话 → {"turnId":…, "last":monotonic}(停滞跟踪)
        self.stall_reported = Seen(500)  # 已触发过停滞续命的 turnId


# ------------------------- 熔断器 -------------------------
def breaker_allow(st, sess):
    """熔断:同会话连续自动发送达上限即抑制,直到该会话出现 turn.completed 复位。
    黑名单挡已知"重试无解"类,熔断兜底未知类(参数惯例参考 claude-code-auto-continue:
    连续 2 次即停,turn 正常完成清零)。"""
    return st.sends_since_success.get(sess, 0) < CIRCUIT_BREAKER_LIMIT


def breaker_record(st, sess):
    st.sends_since_success[sess] = st.sends_since_success.get(sess, 0) + 1


def breaker_success(st, sess):
    st.sends_since_success.pop(sess, None)


# ------------------------- 日志尾随 -------------------------
class Tailer:
    def __init__(self):
        self.path = None
        self.fh = None
        self.offset = None
        self.partial = b""

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None

    def read_new(self, path):
        """返回新完成的 JSON 事件列表。跨天换文件、文件被重建时自动重开;
        新打开的日志文件从文件尾开始读,不追溯既有历史。"""
        events = []
        if self.path != path:
            self.close()
            self.path = path
            self.offset = None
            self.partial = b""
            if os.path.exists(path):
                self.fh = open(path, "rb")
                self.fh.seek(0, 2)
                self.offset = self.fh.tell()
        if self.fh is None:
            if os.path.exists(path):
                self.fh = open(path, "rb")
                self.fh.seek(0, 2)
                self.offset = self.fh.tell()
                return events
            return events
        size = os.path.getsize(path)
        if size < self.offset:  # 文件被截断/重建:丢弃历史,从文件尾继续
            self.close()
            self.fh = open(path, "rb")
            self.fh.seek(0, 2)
            self.offset = self.fh.tell()
            self.partial = b""
            return events
        self.fh.seek(self.offset)
        data = self.fh.read()
        self.offset = size
        if self.partial:
            data = self.partial + data
            self.partial = b""
        if data and not data.endswith(b"\n"):
            cut = data.rfind(b"\n")
            if cut < 0:
                self.partial = data
                return events
            self.partial = data[cut + 1:]
            data = data[:cut + 1]
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line.decode("utf-8", "replace")))
            except ValueError:
                pass
        return events


# ------------------------- 触发发送 -------------------------
def trigger_resume(reason, evt, args):
    """执行一次续命发送。返回是否真正执行了发送(失败不计入熔断
    只应在返回 True 时进行(归位失败/注入失败不算,保留后续报错的重试机会)。"""
    if IS_MACOS:
        return _trigger_resume_macos(reason, evt, args)
    hwnd = find_zcode_hwnd()
    if hwnd is None:
        log("触发失败 (%s, %s) 但找不到 ZCode 窗口,跳过" % (reason, evt.get("sessionId")))
        return False
    sess = evt.get("sessionId") or ""
    target = resolve_session_target(sess) if NAVIGATE_ENABLED else None
    if args.dry_run:
        if not NAVIGATE_ENABLED:
            detail = "归位:关"
        elif target is None:
            detail = "归位:DB解析失败"
        else:
            code = run_nav_script(target, dry=True)
            detail = "归位目标「%s」预演=%s" % (target["title"],
                                             NAV_VERDICTS.get(code, "退出码 %s" % code))
        log("[dry-run] 将发送 '%s' (%s, %s, %s)" % (MESSAGE, reason, sess, detail))
        return False
    focus_done = False
    if NAVIGATE_ENABLED:
        ok, focus_done = navigate_to_session(hwnd, target, sess)
        if not ok:
            focus_done = False
            if not NAV_FAIL_FALLBACK_SEND:
                log("按「宁可不发」策略放弃本次发送 (%s, %s)" % (reason, sess))
                return False
            log("归位未成功,回退发送到当前对话 (%s, %s)" % (reason, sess))
    sent = inject_text(hwnd, MESSAGE, focus_done=focus_done)
    if sent:
        log("已自动发送 '%s' (%s, %s)" % (MESSAGE, reason, sess))
    else:
        log("发送失败: 注入未被接受 (%s, %s)" % (reason, sess))
    return sent


def handle_event_failure(evt, st, args):
    """事件日志路径:turn.failed 判定 → 停滞去重 → turnId 去重 → 熔断 → 发送。
    停滞看门狗已为同一 turnId 续命过时不重复发送。
    节奏由中断自身决定:每次"新的中断"(新 turnId)都独立处理,不设时间门
    —— 中断间隔本来就不固定(取决于 ZCode 的重试耗时),用固定冷却去卡
    只会静默丢掉独立的真实中断(2026-09-11 依据实测数据移除,见复盘 P22)。
    防刷屏由"同 turn 去重 + 每会话熔断"两个事件级守卫承担,不按额度窗口去重。"""
    reason = classify_failure(evt)
    if not reason:
        return
    tid = _event_dedup_key(evt)
    if tid in st.stall_reported.s:
        return
    if not st.turns.add_if_new(tid):
        return
    sess = evt.get("sessionId") or ""
    if not breaker_allow(st, sess):
        log("熔断中,跳过自动发送 (会话 %s, 连续 %d 次未恢复,等 turn.completed 复位)"
            % (sess, st.sends_since_success.get(sess, 0)))
        return
    if trigger_resume(reason, evt, args):
        breaker_record(st, sess)


def track_turn_liveness(evt, st):
    """喂给停滞跟踪与熔断复位:记录 open turn 与最后事件时间;turn.completed
    复位该会话熔断并关闭 open turn(复位不依赖 open turn 存在——监视器可能
    启动晚于 turn.started);turn.failed 仅关闭。subagent 会话不跟踪。"""
    name = evt.get("event")
    sess = evt.get("sessionId") or ""
    if not sess or any(sess.startswith(p) for p in EXCLUDE_SESSION_PREFIXES):
        return
    if name == "turn.started":
        st.open_turns[sess] = {"turnId": evt.get("turnId"), "last": time.monotonic()}
        return
    if name == "turn.completed":
        breaker_success(st, sess)
    ot = st.open_turns.get(sess)
    if ot is None:
        return
    ot["last"] = time.monotonic()
    if name in ("turn.completed", "turn.failed"):
        st.open_turns.pop(sess, None)


def check_stalls(st, args):
    """停滞看门狗(STALL_ENABLED 时每轮调用):open turn 超时无任何事件 → 视为挂起,
    每 turnId 只触发一次续命。后续该 turn 真正 turn.failed 时不再叠加发送。
    与失败路径同样不设固定冷却:不同 turn 属独立中断,各发各的,由熔断封顶。"""
    now = time.monotonic()
    for sess, ot in list(st.open_turns.items()):
        if now - ot["last"] <= STALL_TIMEOUT_SECONDS:
            continue
        st.open_turns.pop(sess, None)
        tid = ot.get("turnId")
        if not tid or not st.stall_reported.add_if_new(tid):
            continue
        if not breaker_allow(st, sess):
            log("熔断中,跳过停滞续命发送 (会话 %s)" % sess)
            continue
        log("检测到疑似停滞 turn(会话 %s, 超过 %ds 无任何事件) → 触发续命"
            % (sess, int(STALL_TIMEOUT_SECONDS)))
        if trigger_resume("stall", {"sessionId": sess, "turnId": tid}, args):
            breaker_record(st, sess)


def handle_quota_record(rec, sess, st, args, allow_judge=True):
    """处理一条 model-io 记录:命中 provider 错误"假成功"(B0/B1/B2 三级判别)
    → 立即续命。顺序:requestId 去重 → 熔断 → 发送;失败不计入熔断,保留重试机会。
    不设固定冷却、也不按"额度重置窗口"去重:同一次报错由 requestId 保证只处理一次,
    不同报错(哪怕重置时间相同)都是独立中断,各发各的,统一由熔断封顶(默认 5 次)。
    额度报错未必是"真耗尽"——中转商路由抖动也会返回同一格式,多试几次常能恢复。"""
    hit = classify_modelio(rec, allow_judge=allow_judge)
    if not hit:
        return
    label, reset_ms, text = hit
    rid = rec.get("requestId")
    if rid and not st.requests.add_if_new(rid):
        return
    if not breaker_allow(st, sess):
        log("熔断中,跳过额度续命发送 (会话 %s)" % sess)
        return
    log("检测到 provider %s: 会话 %s, responseId=%s, 额度重置≈%s"
        % (label, sess, (rec.get("response") or {}).get("responseId"), fmt_reset(reset_ms)))
    sent = trigger_resume("quota", {"sessionId": sess}, args)
    if sent:
        breaker_record(st, sess)
    else:
        log("发送未完成,不计入熔断,后续同类报错可再次尝试 (%s)" % sess)


# ------------------------- 单实例保护 -------------------------
def acquire_single_instance():
    """同名互斥体防止双开;返回持有的句柄,二次启动返回 None。
    macOS 无互斥体,以锁文件(tempfile 目录,含 PID)代替,旧进程死亡后残锁自动失效。"""
    if IS_MACOS:
        import tempfile
        lock = os.path.join(tempfile.gettempdir(), "zcode-resume-watcher.lock")
        if os.path.exists(lock):
            try:
                old = int(open(lock, encoding="utf-8").read().strip())
            except (ValueError, OSError):
                old = 0
            if old:
                try:
                    os.kill(old, 0)
                    return None          # 旧实例仍在运行
                except ProcessLookupError:
                    pass                 # 旧进程已死,残锁 → 接管
                except PermissionError:
                    return None          # 进程存在但无权发信号 → 视为运行中
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return lock
    kernel32x = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32x.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32x.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32x.CreateMutexW(None, False, "Local\\ZCodeResumeWatcher")
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32x.CloseHandle(handle)
        return None
    return handle


# ------------------------- 主流程 -------------------------
def run_watcher(args):
    mutex = acquire_single_instance()
    if mutex is None:
        print("监视器已在运行(双击不要重复启动)。需要停止时关掉之前的黑色窗口即可。")
        sys.exit(0)
    tailer = Tailer()
    rollout_tailers = {}
    st = WatchState()
    log("监视器启动 (平台: %s, 轮询 %ss, %s, 额度监听: %s, 智能归位: %s, 触发: %s, 熔断: %d, 停滞: %s, 心跳: %s)" % (
        "Windows" if IS_WINDOWS else ("macOS" if IS_MACOS else sys.platform),
        args.interval, "dry-run" if args.dry_run else "live",
        "开" if QUOTA_ENABLED else "关",
        ("开," + ("宁可不发" if not NAV_FAIL_FALLBACK_SEND else "失败回退当前对话"))
        if NAVIGATE_ENABLED else "关",
        TRIGGER_MODE, CIRCUIT_BREAKER_LIMIT,
        ("开,%ds" % int(STALL_TIMEOUT_SECONDS)) if STALL_ENABLED else "关",
        ("%dmin" % HEARTBEAT_MINUTES) if HEARTBEAT_MINUTES > 0 else "关"))
    last_heartbeat = time.monotonic()
    while True:
        path = os.path.join(LOG_DIR, "zcode-%s.jsonl" % datetime.date.today().isoformat())
        try:
            events = tailer.read_new(path)
        except OSError as e:
            log("读取日志失败: %s" % e)
            time.sleep(args.interval)
            continue
        for evt in events:
            ev_name = evt.get("event")
            if ev_name == "turn.failed" and args.debug:
                log("turn.failed 事件: %s" % describe_skip(evt))
            track_turn_liveness(evt, st)
            handle_event_failure(evt, st, args)
        # ---- provider 额度"假成功":尾随 rollout/model-io-sess_*.jsonl ----
        if QUOTA_ENABLED:
            live_paths = set()
            for rpath, sess in scan_rollout_files():
                live_paths.add(rpath)
                t = rollout_tailers.get(rpath)
                if t is None:
                    t = rollout_tailers[rpath] = Tailer()  # 新文件从文件尾开始(R3)
                try:
                    records = t.read_new(rpath)
                except OSError as e:
                    log("读取 rollout 日志失败(%s): %s" % (os.path.basename(rpath), e))
                    continue
                for rec in records:
                    handle_quota_record(rec, sess, st, args)
            for gone in [p for p in rollout_tailers if p not in live_paths]:
                rollout_tailers.pop(gone).close()
        if STALL_ENABLED:
            check_stalls(st, args)
        if HEARTBEAT_MINUTES > 0 and time.monotonic() - last_heartbeat >= HEARTBEAT_MINUTES * 60:
            log("心跳: 监视器运行中 (近 %d 分钟无动作,空闲轮询)" % HEARTBEAT_MINUTES)
            last_heartbeat = time.monotonic()
        time.sleep(args.interval)


def once_mode(args):
    """离线回放:自动识别事件日志与 rollout model-io 两种 JSONL,只报告判定,不发送。"""
    total = triggered = 0
    io_total = io_hit = 0
    with open(args.once, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            if not isinstance(evt, dict):
                continue
            if ("requestId" in evt and "sessionId" in evt
                    and isinstance(evt.get("response"), dict)):
                io_total += 1
                hit = classify_modelio(evt, allow_judge=False)  # 回放不联网判官
                if not hit:
                    continue
                label, reset_ms, _ = hit
                io_hit += 1
                print(">>> 应触发 quota(%s) | %s | %s | %s | 额度重置≈%s | responseId=%s"
                      % (label, evt.get("completedAt"), evt.get("sessionId"),
                         evt.get("requestId"), fmt_reset(reset_ms),
                         (evt.get("response") or {}).get("responseId")))
                continue
            if evt.get("event") != "turn.failed":
                continue
            total += 1
            reason = classify_failure(evt)
            if args.debug:
                print("%s | %s | %s" % (evt.get("timestamp"), describe_skip(evt), evt.get("sessionId")))
            if reason:
                triggered += 1
                print(">>> 应触发 %s | %s | %s | %s | %s" % (reason, evt.get("timestamp"),
                                                          evt.get("sessionId"), evt.get("turnId"),
                                                          (evt.get("error") or {}).get("message")))
    print("事件日志: 共 %d 条 turn.failed,符合触发条件 %d 条" % (total, triggered))
    if io_total:
        print("model-io 记录: 共 %d 条,其中 provider 额度报错 %d 条(实际发送还受同窗口抑制/熔断约束)"
              % (io_total, io_hit))


# ------------------------- 覆盖审计(--audit) -------------------------
AUDIT_ACTION_KINDS = (
    ("已自动发送", "发送"),
    ("熔断中", "熔断跳过"),
    ("按「宁可不发」策略放弃", "归位放弃"), ("归位失败", "归位失败"), ("归位存疑", "归位存疑"),
    ("发送失败", "注入失败"), ("发送未完成", "额度未登记"),
    ("检测到 provider", "额度检出"), ("监视器启动", "启动"), ("心跳:", "心跳"),
    ("config.json 已加载", "配置行"),
)
# 错误报文候选的宽预筛(启发式,可能有误报;报告里只是"候选"供人工复核)。
# 要求"error+数字/错误码/code:数字"或额度类关键词,并配合长度上限来降噪。
AUDIT_LOOSE_ENVELOPE = re.compile(
    r"error[\s:：]?\s*\d{2,4}|\"code\"\s*:\s*\"?\d{2,4}|错误码|额度|quota|rate[_ -]?limit|"
    r"reset_?time|重置时间|unavailable|unauthorized|forbidden", re.I)
AUDIT_CAND_MAX_CHARS = 1500   # 超长文本视为正常回答,不列入候选


def _local_naive(iso):
    """带时区/UTC 的 ISO 时间串 → 本地 naive datetime;解析失败返回 None。"""
    if not iso:
        return None
    try:
        return (datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
                .astimezone().replace(tzinfo=None))
    except (TypeError, ValueError, OSError):
        return None


def _parse_watcher_actions(path):
    """解析 watcher.log → [(本地 dt, kind, sessionId, raw)]。kind 见 AUDIT_ACTION_KINDS。"""
    out = []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$", line.rstrip("\n"))
            if not m:
                continue
            try:
                dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            body = m.group(2)
            kind = "其他"
            for key, k in AUDIT_ACTION_KINDS:
                if key in body:
                    kind = k
                    break
            sm = re.search(r"(sess_[0-9a-zA-Z_-]+)", body)
            out.append((dt, kind, sm.group(1) if sm else "", body))
    return out


def _audit_disposition(evt):
    """事件在现行规则下的处置 → (会触发?, 说明)。覆盖全部排除原因,供审计解释。"""
    sess = evt.get("sessionId") or ""
    err = evt.get("error") or {}
    cause = (err.get("cause") or {}).get("code")
    code = err.get("code")
    if any(sess.startswith(p) for p in EXCLUDE_SESSION_PREFIXES):
        return False, "排除:subagent 会话"
    if cause == "model_request_cancelled" or code == "TURN_CANCELLED":
        return False, "排除:用户取消"
    verdict = classify_failure(evt)
    if verdict:
        return True, "触发(%s)" % verdict
    if TRIGGER_MODE != "broad":
        return False, "排除:whitelist 模式未列入白名单"
    if cause in EXCLUDE_ERROR_CAUSES:
        return False, "排除:黑名单 cause=%s(如希望自动续命,从 exclude_error_causes 移除)" % cause
    if code in EXCLUDE_ERROR_CODES:
        return False, "排除:黑名单 code=%s" % code
    return False, "排除:未知(需排查分类逻辑)"


def _audit_event_files(days):
    try:
        names = sorted(n for n in os.listdir(LOG_DIR)
                       if n.startswith("zcode-") and n.endswith(".jsonl"))
    except OSError:
        return []
    return [os.path.join(LOG_DIR, n) for n in names[-max(1, days):]]


def audit_mode(args):
    """离线覆盖审计:扫描近 N 天事件日志 + watcher.log + rollout,报告:
    ① 全部 turn.failed 签名与现行处置  ② 规则↔实际动作交叉核验(漏处理/异常)
    ③ 悬空 turn(started 无终态)        ④ rollout 错误报文候选(B0/B1 未覆盖形状)
    只读,不发送任何消息。返回报告文本(main 负责打印/落盘)。"""
    days = max(1, int(getattr(args, "days", None) or AUDIT_DEFAULT_DAYS))
    rep = []

    def p(s=""):
        rep.append(s)

    now = datetime.datetime.now()
    files = _audit_event_files(days)
    labels = [os.path.basename(f)[6:16] for f in files]
    p("=" * 74)
    p("ZCode 续命监视器 · 覆盖审计报告")
    p("生成时间: %s | 窗口: 近 %d 天 (%s) | 触发模式: %s"
      % (now.strftime("%Y-%m-%d %H:%M:%S"), days,
         ("%s ~ %s" % (labels[0], labels[-1])) if labels else "无日志文件", TRIGGER_MODE))
    p("数据源: %s | %s | %s" % (LOG_DIR, ROLLOUT_DIR, os.path.basename(LOG_FILE)))
    p("=" * 74)

    ev_total = 0
    fails = []                 # [(本地dt, evt)]
    started = {}               # turnId -> (本地dt, sessionId)
    terminal = set()
    for path in files:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"event"' not in line:
                    continue
                m = re.search(r'"event"\s*:\s*"([^"]+)"', line)
                if not m:
                    continue
                ev_total += 1
                name = m.group(1)
                if name not in ("turn.failed", "turn.started", "turn.completed"):
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                tid = evt.get("turnId")
                if name == "turn.started":
                    if tid:
                        started[tid] = (_local_naive(evt.get("timestamp")),
                                        evt.get("sessionId") or "")
                elif tid:
                    terminal.add(tid)
                if name == "turn.failed":
                    fails.append((_local_naive(evt.get("timestamp")), evt))

    p("")
    p("[一] 事件日志规模")
    p("  扫描 %d 个文件, %d 条事件, turn.failed %d 条" % (len(files), ev_total, len(fails)))

    # ---- [二] 签名与处置 ----
    p("")
    p("[二] turn.failed 全签名与现行处置(broad 模式:未列入黑名单且非取消/subagent 一律触发)")
    sigs = {}
    for dtl, evt in fails:
        err = evt.get("error") or {}
        key = ((err.get("cause") or {}).get("code"), err.get("code"))
        d = sigs.setdefault(key, {"n": 0, "first": None, "last": None, "sample": None})
        d["n"] += 1
        if dtl:
            if d["first"] is None or dtl < d["first"]:
                d["first"] = dtl
            if d["last"] is None or dtl > d["last"]:
                d["last"] = dtl
        if d["sample"] is None:
            d["sample"] = evt
    if not sigs:
        p("  (窗口内无 turn.failed)")
    for key in sorted(sigs, key=lambda k: -sigs[k]["n"]):
        d = sigs[key]
        trig, why = _audit_disposition(d["sample"])
        rng = ""
        if d["first"] and d["last"]:
            rng = "  首见 %s 末见 %s" % (d["first"].strftime("%m-%d %H:%M"),
                                       d["last"].strftime("%m-%d %H:%M"))
        p("  %s %-44s x%-3d%s" % ("★触发" if trig else "   排除",
                                  "%s / %s" % (key[0], key[1]), d["n"], rng))
        p("        处置: %s" % why)
        ex = d["sample"]
        err = ex.get("error") or {}
        exl = _local_naive(ex.get("timestamp"))
        p("        样例: %s %s | %s"
          % ((exl.strftime("%m-%d %H:%M:%S") if exl else str(ex.get("timestamp"))[:19]),
             (ex.get("sessionId") or "")[:36],
             str(err.get("message"))[:70]))

    # ---- [三] 规则 ↔ 实际动作 ----
    p("")
    p("[三] 规则↔实际动作交叉核验(应触发事件 vs watcher.log 处理记录)")
    actions = _parse_watcher_actions(LOG_FILE)
    stats = collections.Counter()
    unattended = []
    running_anomaly = []
    for dtl, evt in fails:
        trig, _why = _audit_disposition(evt)
        if not trig or dtl is None:
            continue
        sess = evt.get("sessionId") or ""
        win = [a for a in actions if a[1] != "配置行"
               and -AUDIT_WINDOW_BEFORE <= (a[0] - dtl).total_seconds() <= AUDIT_WINDOW_AFTER]
        if any(a[1] == "发送" and (not a[2] or a[2] == sess) for a in win):
            stats["已发送"] += 1
            continue
        skip = [a for a in win if a[1] in ("熔断跳过", "归位放弃", "归位失败",
                                           "归位存疑", "注入失败", "额度未登记")]
        if skip:
            stats[skip[0][1]] += 1
        elif win:
            stats["运行中-无处理记录(需排查)"] += 1
            running_anomaly.append((dtl, evt))
        else:
            stats["未处理(监视器未运行/无响应)"] += 1
            unattended.append((dtl, evt))
    p("  应触发 %d 条 → %s" % (sum(stats.values()),
                              ", ".join("%s %d" % kv for kv in stats.most_common())))
    if unattended:
        p("  未处理明细(应触发但日志无任何处理记录——监视器未运行或无响应):")
        for dtl, evt in unattended[:10]:
            cause = ((evt.get("error") or {}).get("cause") or {}).get("code")
            prev = [a for a in actions if a[1] == "启动" and a[0] <= dtl]
            ctx = "窗口内无启动记录"
            if prev:
                ctx = "最近一次启动 %s(早于事件 %.1f 小时)" % (
                    prev[-1][0].strftime("%m-%d %H:%M"),
                    (dtl - prev[-1][0]).total_seconds() / 3600.0)
            p("    %s %s %s → %s" % (dtl.strftime("%m-%d %H:%M:%S"),
                                    (evt.get("sessionId") or "")[:30], cause, ctx))
    if running_anomaly:
        p("  ⚠ 运行中却无处理记录(疑似处理链路问题,请排查):")
        for dtl, evt in running_anomaly[:10]:
            p("    %s %s" % (dtl.strftime("%m-%d %H:%M:%S"), (evt.get("sessionId") or "")[:30]))
    p("  心跳: heartbeat_minutes=%s → %s" % (HEARTBEAT_MINUTES,
        "未来审计可按心跳间隔精确判定运行窗口" if HEARTBEAT_MINUTES > 0 else
        "关闭(空闲期无日志,运行窗口只能粗略推断;建议开启)"))

    # ---- [四] 悬空 turn ----
    p("")
    p("[四] 悬空 turn(turn.started 无 completed/failed;跨天文件已合并统计)")
    dangling = []
    for tid, (dtl, sess) in started.items():
        if tid in terminal or dtl is None:
            continue
        dangling.append(((now - dtl).total_seconds(), dtl, sess, tid))
    dangling.sort(reverse=True)
    real = [d for d in dangling if d[0] > STALL_TIMEOUT_SECONDS]
    inflight = [d for d in dangling if d[0] <= STALL_TIMEOUT_SECONDS]
    p("  真悬空候选(超过停滞阈值 %ds): %d" % (int(STALL_TIMEOUT_SECONDS), len(real)))
    for age, dtl, sess, tid in real[:8]:
        p("    %s %s %s 已 %.1f 小时无终态"
          % (dtl.strftime("%m-%d %H:%M:%S"), sess[:30], tid[:26], age / 3600.0))
    p("  在飞(未超阈值,正常): %d" % len(inflight))
    p("  停滞看门狗: %s" % ("已开启" if STALL_ENABLED else
                            "关闭(stall_enabled=false);开启后可自动处理此类挂起"))

    # ---- [五] rollout 候选 ----
    p("")
    p("[五] rollout 错误报文候选(B0/B1 未覆盖的形状;启发式粗筛)")
    io_total = covered = empty = 0
    cands, seen_txt = [], set()
    for rpath, sess in scan_rollout_files():
        try:
            fh = open(rpath, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"response"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not isinstance(rec.get("response"), dict):
                    continue
                io_total += 1
                if classify_modelio(rec, allow_judge=False):
                    covered += 1
                    continue
                text = (rec.get("response") or {}).get("text")
                if not isinstance(text, str) or not text.strip():
                    empty += 1
                    continue
                if len(text) <= AUDIT_CAND_MAX_CHARS and AUDIT_LOOSE_ENVELOPE.search(text):
                    key = text.strip()[:60]
                    if key in seen_txt:
                        continue
                    seen_txt.add(key)
                    cands.append((sess, str((rec.get("response") or {}).get("responseId"))[:36],
                                  text.strip()[:100]))
    p("  扫描记录 %d: B0/B1 命中 %d, 空文本噪声 %d, 错误报文候选 %d"
      % (io_total, covered, empty, len(cands)))
    for sess, rid, t in cands[:10]:
        p("    候选: sess=%s rid=%s" % (sess[:30], rid))
        p("          text=%r" % t)
    if cands:
        p("  说明: 候选为关键词粗筛,可能含讨论错误的正常回答;是否纳入 quota_needles 或调整")
        p("        quota_text_regex 由人工复核,或启用 B2 判官(judge_enabled)在线甄别")

    # ---- [六] 建议 ----
    p("")
    p("[六] 建议")
    tips = []
    if unattended:
        tips.append("有 %d 条应触发事件因监视器未运行被漏:双击 启动自动续命.bat 常驻(最小化即可)"
                    % len(unattended))
    if running_anomaly:
        tips.append("有 %d 条运行中却无处理记录:需排查处理链路(可能是新 bug)" % len(running_anomaly))
    if real:
        tips.append("有 %d 个真悬空 turn:可考虑开启 stall_enabled=true(建议先 --dry-run 观察)" % len(real))
    if cands:
        tips.append("rollout 有 %d 个错误报文候选:复核后可扩展 quota_needles 或调整 quota_text_regex"
                    % len(cands))
    black = []
    for key, d in sigs.items():
        if d["sample"] is None:
            continue
        trig, why = _audit_disposition(d["sample"])
        if not trig and why.startswith("排除:黑名单"):
            black.append(key[0] or key[1])
    if black:
        tips.append("被排除的签名: %s —— 如需自动续命请调整 exclude_error_causes/codes 或 trigger_mode"
                    % ", ".join(sorted(str(b) for b in black)))
    if tips:
        for t in tips:
            p("  ▸ " + t)
    else:
        p("  (无异常项: 覆盖正常)")
    p("")
    p("本报告只读,未发送任何消息。复跑: python zcode_resume_watcher.py --audit --days %d" % days)
    return "\n".join(rep)


def main():
    ap = argparse.ArgumentParser(description="ZCode 断连自动续命监视器")
    ap.add_argument("--version", action="version", version="v%s" % VERSION)
    ap.add_argument("--config", metavar="JSON", help="指定 config.json 路径(默认脚本同目录),便于测试/迁移")
    ap.add_argument("--dry-run", action="store_true", help="只记录将发送,不真正打字")
    ap.add_argument("--interval", type=float, default=None, help="日志轮询间隔秒数(默认取配置 poll_seconds)")
    ap.add_argument("--debug", action="store_true", help="打印每条 turn.failed 的分类")
    ap.add_argument("--once", metavar="JSONL", help="只扫描指定 JSONL 文件并报告触发,不发送")
    ap.add_argument("--check-updates", action="store_true", help="查询 GitHub 最新版本后退出")
    ap.add_argument("--test-send", action="store_true", help="立即向 ZCode 输入框测试发送一次并退出")
    ap.add_argument("--test-navigate", metavar="SESSION_ID",
                    help="智能归位演练:解析会话标题→侧栏定位→点击验证→聚焦输入框,不发送消息")
    ap.add_argument("--audit", action="store_true",
                    help="覆盖审计:扫描近 N 天日志,报告错误签名/漏处理/悬空 turn/rollout 候选(只读不发送)")
    ap.add_argument("--days", type=int, default=AUDIT_DEFAULT_DAYS,
                    help="审计窗口天数(默认 %d,仅与 --audit 连用)" % AUDIT_DEFAULT_DAYS)
    ap.add_argument("--out", metavar="FILE", help="审计报告同时写入该文件(仅与 --audit 连用)")
    args = ap.parse_args()
    load_config(args.config)
    if args.check_updates:
        check_updates()
        return
    if CHECK_UPDATES:
        check_updates()
    if args.interval is None:
        args.interval = POLL_SECONDS
    if args.audit:
        report = audit_mode(args)
        print(report, flush=True)
        if args.out:
            try:
                with open(args.out, "w", encoding="utf-8") as f:
                    f.write(report + "\n")
                print("报告已写入: %s" % args.out, flush=True)
            except OSError as e:
                print("报告写入失败: %s" % e, flush=True)
        return
    if args.once:
        once_mode(args)
        return
    if args.test_navigate:
        if IS_MACOS:
            print("macOS 暂不支持智能归位(v1 限制,见 README)", flush=True)
            sys.exit(9)
        sess = args.test_navigate
        hwnd = find_zcode_hwnd()
        if hwnd is None:
            print("找不到 ZCode 窗口", flush=True)
            sys.exit(1)
        target = resolve_session_target(sess)
        if target is None:
            print("DB 解析失败: 会话 %s 无标题记录或库不可读" % sess, flush=True)
            sys.exit(2)
        print("目标: 「%s」 项目=%s time_updated=%s" % (
            target["title"], target["project"], target["time_updated"]), flush=True)
        ok, focused = navigate_to_session(hwnd, target, sess)
        print("归位结果: %s (导航脚本已聚焦输入框=%s)" % ("成功" if ok else "失败", focused), flush=True)
        if ok and not focused:
            print("普通聚焦补偿结果:", uia_focus_input(), flush=True)
        sys.exit(0 if ok else 3)
    if args.test_send:
        if IS_MACOS:
            print("macOS 测试发送 '%s' (Ctrl+C 取消;需先授权辅助功能权限)" % MESSAGE, flush=True)
            time.sleep(3)
            print("发送结果: %s" % mac_inject_text(MESSAGE), flush=True)
            return
        hwnd = find_zcode_hwnd()
        if hwnd is None:
            print("找不到 ZCode 窗口", flush=True)
            sys.exit(1)
        print("找到 ZCode 窗口 %s,10 秒后测试发送 '%s' (Ctrl+C 取消)" % (hwnd, MESSAGE), flush=True)
        time.sleep(10)
        print("发送结果: %s" % inject_text(hwnd, MESSAGE), flush=True)
        return
    run_watcher(args)


if __name__ == "__main__":
    main()
