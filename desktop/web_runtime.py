# -*- coding: utf-8 -*-
"""Web 面板的运行时数据：从 watcher.log 重建运行区间、恢复记录与会话。

纯标准库；不 import UI / Windows API。它既作为只读预览的数据源，
也作为真实桌面 app 里引擎 ``log()`` observer 的数据源。

设计原则：
- 日志是唯一事实来源；引擎判定/守卫逻辑不改。
- 只把"最终结果行"当作一次恢复的终点（发送成功/未确认送达/熔断/暂停/放弃），
  中间的归位/注入失败行作为上下文，不单独计为一条记录。
- 记录以事件身份（时间 + 会话 + 结果）为键，不做时间窗口去重。
"""
import datetime
import re
import threading
from collections import deque

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$")
_SESS = re.compile(r"(sess_[0-9a-zA-Z_-]+)")
_TITLE = re.compile(r"「(.+?)」")
_QUOTED = re.compile(r"'([^']*)'")
_PROVIDER = re.compile(r"检测到 provider (.+?):\s*会话")
_ATTEMPTS = re.compile(r"已尝试 (\d+) 次")
_BREAKER = re.compile(r"连续 (\d+) 次未恢复")
_REASON = re.compile(r"\(([a-zA-Z_]+),\s*(sess_[0-9a-zA-Z_-]+)\)")

_REASONS = {
    "connection": ("connection", "连接重试耗尽"),
    "empty_response": ("empty_response", "空响应"),
    "quota": ("quota", "额度报错"),
    "stall": ("stall", "turn 停滞"),
    "turn_failed": ("turn_failed", "中断"),
}

_OUTCOME_LINES = (
    "已自动发送并确认送达",
    "已自动发送",
    "⚠ 续命未确认送达",
    "熔断中,跳过自动发送",
    "熔断中,跳过停滞续命发送",
    "熔断中,跳过额度续命发送",
    "按「宁可不发」策略放弃",
    "已暂停,跳过自动发送",
)

# 这些"失败"实际上没有发送，归为拦截而不是未送达。
_GUARD_REASONS = (
    "草稿", "未被识别", "焦点未落到", "归位未成功", "宁可不发",
    "输入框未就绪", "找不到焦点",
)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_ts(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


class WebRuntime:
    """线程安全的 Web 面板状态。

    ``resolver(session_id)`` 可选，用于补齐会话标题/项目（读 ZCode 本地库）；
    ``message`` 是面板里展示的续命消息。
    """

    def __init__(self, version="", message="任务意外中断，请继续！",
                 resolver=None, now=None, breaker_limit=5):
        self.version = version
        self.message = message
        self.breaker_limit = max(1, int(breaker_limit))
        self._resolver = resolver
        self._now = now or datetime.datetime.now
        self._lock = threading.RLock()
        self._recent = deque(maxlen=120)
        self._runs = []
        self._recoveries = []
        self._sessions = {}
        self._resolve_cache = {}
        self._last_outcome_ts = None
        self._last_heartbeat = None
        self._last_exit = None
        self._last_exit_missing = False
        self._first_run = False
        self._current_run = None
        self._current_run_start = None
        self._seq = 0
        self._rev = 0
        self.state = "starting"
        self.read_only = False
        self.error = ""
        self._seeded = False

    # ------------------------------------------------------------------
    # 输入
    # ------------------------------------------------------------------
    def seed_from_log(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            self._first_run = True
            self._update_state()
            return
        for line in lines:
            self._ingest_line(line)
        self._seeded = True
        self._update_state()

    def observe(self, line):
        """接收引擎 log() 的整行输出；异常绝不影响主流程。"""
        return self._ingest_line(line)

    def _ingest_line(self, line):
        line = (line or "").rstrip("\n")
        m = _TS.match(line)
        if not m:
            return None
        ts = _parse_ts(m.group(1))
        if ts is None:
            return None
        body = m.group(2)
        with self._lock:
            self._rev += 1
            self._ingest(ts, body, line)
            self._update_state()
        return None

    def _ingest(self, ts, body, raw):
        # 心跳
        if body.startswith("心跳:"):
            self._last_heartbeat = ts
        # 运行区间
        if "监视器启动" in body:
            self._start_run(ts)
        elif body.startswith("⚠ 监视器退出:"):
            self._end_run(ts, body.split("监视器退出:", 1)[-1].strip())
        # 只把最终结果行作为一次恢复
        outcome = self._outcome_of(body)
        if outcome:
            self._add_recovery(ts, body, raw, outcome)
            self._last_outcome_ts = ts
        # 保留上下文（结果行也保留，供下一次窗口边界使用）
        self._recent.append((ts, body, raw))

    # ------------------------------------------------------------------
    # 运行区间
    # ------------------------------------------------------------------
    def _start_run(self, ts):
        prev_open = self._current_run is not None
        had_runs = bool(self._runs)
        if prev_open:
            # 上一个进程没有留下退出遗言：标记 no-note，结束时间用本次启动近似
            prev = self._current_run
            prev["end"] = _iso(ts)
            prev["note"] = False
            prev["exit"] = None
            prev["current"] = False
            self._last_exit = None
            self._last_exit_missing = True
        elif had_runs:
            # 上一轮正常退出，_end_run 已写入 last_exit
            self._last_exit_missing = False
        else:
            self._first_run = True
            self._last_exit_missing = False
        self._first_run = not had_runs and not prev_open
        run = {
            "id": "run-%d" % (len(self._runs) + 1),
            "start": _iso(ts),
            "end": None,
            "exit": None,
            "note": True,
            "current": True,
        }
        self._runs.append(run)
        self._current_run = run
        self._current_run_start = ts

    def _end_run(self, ts, reason):
        if self._current_run is None:
            return
        run = self._current_run
        run["end"] = _iso(ts)
        run["exit"] = reason
        run["note"] = True
        run["current"] = False
        self._current_run = None
        self._last_exit = {"at": _iso(ts), "reason": reason, "noNote": False}
        self._last_exit_missing = False

    def _update_state(self):
        if self.state in ("error", "paused"):
            return
        self.state = "running" if self._current_run is not None else (
            "stopped" if self._runs else "starting")

    def set_state(self, state, error=""):
        with self._lock:
            self.state = state
            self.error = error or ""
            self._rev += 1

    def tick(self):
        with self._lock:
            self._rev += 1

    # ------------------------------------------------------------------
    # 恢复记录
    # ------------------------------------------------------------------
    @staticmethod
    def _outcome_of(body):
        if "⚠ 续命未确认送达" in body:
            return "failed_or_guard"
        if "已自动发送并确认送达" in body:
            return "delivered"
        if "已自动发送" in body:
            return "unverified"
        if "熔断中,跳过" in body:
            return "guarded"
        if "按「宁可不发」策略放弃" in body:
            return "guarded"
        if "已暂停,跳过自动发送" in body:
            return "guarded"
        return None

    def _ctx(self, ts):
        since = ts - datetime.timedelta(seconds=120)
        if self._last_outcome_ts is not None:
            since = max(since, self._last_outcome_ts +
                        datetime.timedelta(microseconds=1))
        return [x for x in self._recent if x[0] > since]

    def _add_recovery(self, ts, body, raw, outcome):
        ctx = self._ctx(ts) + [(ts, body, raw)]
        sess = ""
        for _t, b, _r in reversed(ctx):
            m = _SESS.search(b)
            if m:
                sess = m.group(1)
                break
        # 只从归位行取标题；守卫原因里的「…」不是会话标题。
        title = ""
        for _t, b, _r in reversed(ctx):
            if "归位成功" not in b and "归位失败" not in b:
                continue
            m = _TITLE.search(b)
            if m:
                title = m.group(1).strip()
                break
        detect = None
        for _t, b, _r in ctx:
            if "检测到 provider" in b:
                m = _PROVIDER.search(b)
                detect = (_t, b, (m.group(1).strip() if m else "额度报错"))
        reason_m = None
        for _t, b, _r in reversed(ctx):
            reason_m = _REASON.search(b)
            if reason_m:
                break
        reason_key = reason_m.group(1) if reason_m else ""
        kind, label = _REASONS.get(reason_key, ("other", "中断"))
        detect_detail = label
        if detect:
            detect_detail = "provider 额度报错 · %s" % detect[2]
            kind, label = "quota", "额度报错"
        elif "检测到疑似停滞" in "".join(b for _t, b, _r in ctx):
            kind, label = "stall", "turn 停滞"
            detect_detail = "turn 长时间无事件"

        guard = None
        breaker = None
        for _t, b, _r in reversed(ctx):
            m = _BREAKER.search(b)
            if m:
                breaker = int(m.group(1))
                break
        if outcome == "guarded":
            if breaker is not None:
                guard = "同会话连续 %d 次未恢复，熔断；等 turn.completed 复位" % breaker
                detect_detail = "同会话连续 %d 次未恢复" % breaker
                kind, label = "breaker", "熔断"
            elif "已暂停" in body:
                guard = "用户暂停，只记录不发送"
                detect_detail = "用户暂停"
                kind, label = "paused", "暂停"
            else:
                guard = body.split(":", 1)[-1].strip() or "守卫放弃发送"
                detect_detail = guard
                kind, label = "guard", "拦截"
            final = "guarded"
        elif outcome == "failed_or_guard":
            reason_tail = body.split("末次原因「", 1)[-1].split("」", 1)[0] if "末次原因「" in body else ""
            if any(x in reason_tail for x in _GUARD_REASONS):
                final = "guarded"
                guard = reason_tail or "守卫放弃发送"
            else:
                final = "failed"
        else:
            final = outcome

        # 时间原点：provider 检测行 > 最近一次归位行 > 结果行
        nav = None
        for _t, b, _r in reversed(ctx):
            if "归位成功" in b or "归位失败" in b or "归位未成功" in b:
                nav = (_t, b)
                break
        at = detect[0] if detect else (nav[0] if nav else ts)
        total_ms = max(0, int((ts - at).total_seconds() * 1000))

        # 四段链路
        stages = []
        stages.append({
            "key": "detect", "status": "done", "ms": 0,
            "detail": detect_detail,
        })
        nav_ok = None
        if nav:
            b = nav[1]
            if "归位成功但脚本未见输入框" in b:
                nav_ok = ("done", "脚本未见输入框 · 走普通聚焦")
            elif "归位成功" in b:
                nav_ok = ("done", "侧栏定位 · 顶栏标题逐字验证通过")
            else:
                detail = b.split(":", 1)[-1].strip()
                detail = _TITLE.sub("", detail)
                detail = detail.replace("(详见 navigate.log)", "").strip(" ·:：")
                nav_ok = ("failed", detail[:120] or "归位失败")
        if final == "guarded" and not nav:
            stages.append({"key": "navigate", "status": "skipped", "ms": 0,
                           "detail": "未尝试归位"})
        elif nav_ok:
            status, detail = nav_ok
            ms = max(0, int((nav[0] - at).total_seconds() * 1000))
            stages.append({"key": "navigate", "status": status, "ms": ms,
                           "detail": detail})
        else:
            stages.append({"key": "navigate", "status": "skipped", "ms": 0,
                           "detail": "未尝试归位"})

        reason_tail = body.split("末次原因「", 1)[-1].split("」", 1)[0] if "末次原因「" in body else ""
        if final == "guarded":
            stages.append({"key": "inject", "status": "skipped", "ms": 0, "detail": "—"})
            stages.append({"key": "verify", "status": "skipped", "ms": 0, "detail": "—"})
        elif final in ("delivered", "unverified"):
            stages.append({"key": "inject", "status": "done", "ms": 0,
                           "detail": "WM_CHAR 逐字投递 · 回车"})
            if final == "delivered":
                stages.append({"key": "verify", "status": "done",
                               "ms": max(0, total_ms -
                                         sum(x["ms"] for x in stages)),
                               "detail": "库内确认消息已入会话"})
            else:
                detail = "已投递 · 未核验（库不可读）"
                if "核验关闭" in body:
                    detail = "已投递 · 核验关闭"
                stages.append({"key": "verify", "status": "unknown", "ms": 0,
                               "detail": detail})
        else:  # failed
            inject_failed = any(x in reason_tail for x in
                                ("输入框", "焦点", "注入未执行", "注入未被接受"))
            if inject_failed:
                stages.append({"key": "inject", "status": "failed", "ms": 0,
                               "detail": reason_tail})
            else:
                stages.append({"key": "inject", "status": "done", "ms": 0,
                               "detail": "WM_CHAR 已投递"})
            stages.append({"key": "verify", "status": "failed",
                           "ms": max(0, total_ms -
                                     sum(x["ms"] for x in stages)),
                           "detail": reason_tail or "未确认送达"})

        # 会话标题/项目（读库是可选增强）
        sess_title = title
        project = "—"
        target = None
        # 只给面板会显示的近 24h 记录查库;历史记录不查,避免启动时几百次 SQLite。
        recent = ts >= (self._now() - datetime.timedelta(hours=25))
        if self._resolver is not None and sess and recent:
            if sess not in self._resolve_cache:
                try:
                    self._resolve_cache[sess] = self._resolver(sess)
                except Exception:
                    self._resolve_cache[sess] = None
            target = self._resolve_cache.get(sess)
            if target:
                sess_title = sess_title or target.get("title") or ""
                project = target.get("project") or project
        old = self._sessions.get(sess) if sess else None
        if old:
            sess_title = sess_title or old.get("title") or ""
            project = project if project != "—" else old.get("project", "—")
        if sess:
            if breaker and breaker >= self.breaker_limit:
                sstate = "blocked"
            elif breaker or final in ("failed", "guarded", "unverified"):
                sstate = "attention"
            else:
                sstate = "healthy"
            self._sessions[sess] = {
                "id": sess,
                "title": sess_title or sess,
                "project": project,
                "state": sstate,
                "breaker": breaker or 0,
                "lastAt": _iso(ts),
                "recoveryIds": list((old or {}).get("recoveryIds", [])),
            }

        attempts = 0
        m = _ATTEMPTS.search(body)
        if m:
            attempts = int(m.group(1))
        elif final in ("delivered", "unverified"):
            attempts = 1
        message = self.message
        m = _QUOTED.search(body)
        if m:
            message = m.group(1)

        self._seq += 1
        rec = {
            "id": "r-%d" % self._seq,
            "at": _iso(at),
            "sessionId": sess,
            "trigger": {"kind": kind, "label": label,
                        "detail": detect_detail},
            "outcome": final,
            "totalMs": total_ms,
            "guard": guard,
            "stages": stages,
            "evidence": {
                "message": message,
                "turnId": "",
                "requestId": "",
                "event": detect_detail,
                "attempts": attempts,
                "deliveredAt": _iso(ts) if final == "delivered" else None,
            },
            "raw": "\n".join(r for _t, _b, r in ctx),
        }
        self._recoveries.append(rec)
        if len(self._recoveries) > 800:
            self._recoveries = self._recoveries[-800:]
        if sess and sess in self._sessions:
            ids = self._sessions[sess]["recoveryIds"]
            ids.append(rec["id"])
            del ids[:-100]

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def snapshot(self):
        now = self._now()
        with self._lock:
            cur_start = self._current_run_start
            uptime = int((now - cur_start).total_seconds()) if cur_start else 0
            hb = self._last_heartbeat
            # 上一轮进程的心跳不能冒充本轮心跳:显示"—"直到本次启动后第一次心跳。
            if hb is not None and cur_start is not None and hb < cur_start:
                hb = None
            heartbeat_age = int((now - hb).total_seconds()) if hb else None
            cutoff = now - datetime.timedelta(hours=24)
            recs = [r for r in self._recoveries
                    if _parse_ts(r["at"].replace("T", " ")) is not None
                    and _parse_ts(r["at"].replace("T", " ")) >= cutoff]
            recs.sort(key=lambda r: r["at"], reverse=True)
            runs = [dict(r) for r in self._runs
                    if r["end"] is None or
                    (_parse_ts(r["end"].replace("T", " ")) is not None
                     and _parse_ts(r["end"].replace("T", " ")) >=
                     now - datetime.timedelta(days=8))]
            sessions = []
            for s in self._sessions.values():
                s = dict(s)
                s["recoveryIds"] = list(s.get("recoveryIds", []))[-100:]
                sessions.append(s)
            sessions.sort(key=lambda s: s.get("lastAt", ""), reverse=True)
            return {
                "version": self.version,
                "breakerLimit": self.breaker_limit,
                "now": _iso(now),
                "state": self.state,
                "error": self.error,
                "readOnly": self.read_only,
                "revision": self._rev,
                "uptimeSec": uptime,
                "heartbeatAgeSec": heartbeat_age,
                "lastExit": dict(self._last_exit) if self._last_exit else None,
                "lastExitMissing": self._last_exit_missing,
                "firstRun": self._first_run,
                "runs": runs,
                "recoveries": recs,
                "sessions": sessions,
            }
