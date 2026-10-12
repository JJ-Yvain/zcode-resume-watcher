# -*- coding: utf-8 -*-
"""zcode-resume-watcher 合成回归测试(不发送、不碰真实日志)。
覆盖:额度"假成功"三级判别(T1-T13,T18-T20)、broad 全量接管/黑名单/熔断/停滞(T14-T17,T21)、
覆盖审计 --audit 机制(T22-T25)、上下文压缩自指误报(T29)、送达核验+有界重试+输入框身份判据(T30)、
退出取证(遗言,T31)、外部评审确认的输入/配置形状防御(T32-T33)、macOS 触发链同款核验(T34)、
焦点落地核验(SetFocus 被忽略时拒绝盲打字,T35)。
三平台 CI 都会跑本文件:平台相关断言分 Win / 非 Win 两支,ps1 编码闸门在无 PowerShell 的机器上显式跳过。
运行: python driver.py   → 全 PASS 退出码 0。"""
import datetime
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows runner stdout 可能是 cp1252

SCR = os.path.dirname(os.path.abspath(__file__))            # 必须先定义:公共副本用 PROJ = os.path.dirname(SCR)
PROJ = os.path.dirname(SCR)   # public repo layout: tests/ sits next to the main script
ROLL = os.path.join(SCR, "rollout")
shutil.rmtree(ROLL, ignore_errors=True)
os.makedirs(ROLL)

spec = importlib.util.spec_from_file_location("zrw", os.path.join(PROJ, "zcode_resume_watcher.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

LOGS = []
w.log = lambda msg: LOGS.append(msg)          # 捕获运行日志
w.LOG_FILE = os.path.join(SCR, "test-watcher.log")
w.load_config()                                # 用正式默认规则 needles/regex
w.ROLLOUT_DIR = ROLL                           # 重定向到 scratch(必须在 load_config 之后)
w.CIRCUIT_BREAKER_LIMIT = 99                   # T1-T13 保持原发送计数语义;熔断在 T17/T21c 单测

class Args(object):
    dry_run = True
    debug = False

st = w.WatchState()
PASS = []
def check(name, cond, evidence=""):
    PASS.append((name, bool(cond)))
    print("%-4s %s %s" % ("PASS" if cond else "FAIL", name, ("| " + evidence) if evidence else ""))

# P1-2 新语义:窗口登记/冷却只在"真正发送成功"后登记。driver 用可切换的 mock 模拟
# trigger_resume 的发送结果(默认 True=live 发送成功),断言从"日志计数"升级为"发送调用计数"。
SENT = []
w.trigger_resume = lambda reason, evt, a: (SENT.append(evt.get("sessionId")), True)[1]

TEXT = '\n[qoder error 403: {"code":"115","message":"{\\"agentLimitResetTime\\":1789284070224}"}]'

def mk(rid, resp_text, echo=False):
    msgs = [{"role": "assistant", "content": TEXT if echo else "正常历史"}]
    return {"completedAt": "2026-08-31T15:00:00.000Z", "requestId": rid,
            "attempt": 1, "model": {"modelId": "monk"},
            "request": {"body": {"model": "monk", "messages": msgs}},
            "response": {"finishReason": "stop", "responseId": "resp-" + rid,
                         "text": resp_text, "toolCalls": [], "usage": {}},
            "sessionId": "sess_synthetic", "turnId": "turn_synth"}

def append_line(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def read_and_handle(tailer, path):
    for rec in tailer.read_new(path):
        w.handle_quota_record(rec, "sess_synthetic", st, Args)

def nlog(sub):
    return sum(1 for l in LOGS if sub in l)

path = os.path.join(ROLL, "model-io-sess_synthetic.jsonl")

# T1 冷启动不追溯:文件里先有一条报错,新建 Tailer 不应读到
append_line(path, mk("r0", TEXT))
t = w.Tailer()
check("T1 冷启动 seek 尾不追溯历史", t.read_new(path) == [])

# T2 新出现的额度记录 → 发送被调用并登记窗口
append_line(path, mk("r1", TEXT))
read_and_handle(t, path)
check("T2 首次额度报错立即触发", nlog("检测到 provider 额度报错") == 1 and len(SENT) == 1)

# T3(2026-09-11 重写)额度窗口抑制已移除:同窗口的新报错也是独立中断,照发
append_line(path, mk("r2", TEXT))
read_and_handle(t, path)
read_and_handle(t, path)  # 幂等:无新行不应产生任何新日志
check("T3 同额度窗口新报错照发(每条独立中断一次)",
      len(SENT) == 2 and nlog("防刷屏跳过") == 0)

# T4 requestId 重复行(模拟重复读)→ 静默
n0 = len(LOGS)
for rec in [json.loads(json.dumps(mk("r1", TEXT)))]:
    w.handle_quota_record(rec, "sess_synthetic", st, Args)
check("T4 requestId 去重静默", len(LOGS) == n0)

# T5 更晚的 reset 时间(新额度窗口)→ 再次触发
TEXT2 = TEXT.replace("1789284070224", "1790000000000")
append_line(path, mk("r3", TEXT2))
read_and_handle(t, path)
check("T5 新额度窗口再次触发", len(SENT) == 3)

# T6 回显行(request 带错误文本、response 正常)→ 不触发
append_line(path, mk("r4", "好的,继续完成任务。", echo=True))
read_and_handle(t, path)
check("T6 request 回显零误报", len(SENT) == 3 and t.read_new(path) == [])

# T7(2026-09-11 重写)固定冷却已移除:改为验证"事件驱动"语义——
# 没有时间门,新额度窗口即时发送;窗口抑制仍是唯一防线。
TEXT3 = TEXT.replace("1789284070224", "1791000000000")
append_line(path, mk("r5", TEXT3))         # 新窗口 A
read_and_handle(t, path)
check("T7a 无冷却:新额度窗口立即发送", len(SENT) == 4)
append_line(path, mk("r6", TEXT3))         # 同窗口 A 的新记录 → 独立中断,照发
read_and_handle(t, path)
check("T7b 同窗口再现照发(无窗口抑制/无冷却)", len(SENT) == 5)
TEXT3b = TEXT.replace("1789284070224", "1791500000000")
append_line(path, mk("r6b", TEXT3b))       # 又一个新窗口 B → 应立刻再发
read_and_handle(t, path)
check("T7c 紧接着的另一个新窗口照发(不被时间门拦住)", len(SENT) == 6)

# T8 无时间戳兜底:解析不到 resetMs 用文本哈希,同文本只发一次
TEXT_NO_MS = '\n[qoder error 403: {"code":"115","message":"agentLimitResetTime unknown"}]'
append_line(path, mk("r7", TEXT_NO_MS))
read_and_handle(t, path)
sent_before = len(SENT)
append_line(path, mk("r8", TEXT_NO_MS))
read_and_handle(t, path)
check("T8 无时间戳哈希:两条同文本报错各发一次", sent_before == 7 and len(SENT) == 8)

# T9 变体转义(无内层反斜杠)也能命中并解析
TEXT_V = '\n[qoder error 403: {"code":"115","message":"agentLimitResetTime:1789284070224"}]'
hit = w.find_quota_hit(mk("rv", TEXT_V))
check("T9 变体文本命中+解析 reset", hit is not None and hit[0] == 1789284070224)

# T10 fmt_reset 本地时区换算(时区无关断言:期望值经 UTC→本地 独立换算路径得出)
import datetime as _dt
_exp = _dt.datetime.fromtimestamp(1789284070224 / 1000.0, _dt.timezone.utc).astimezone()
check("T10 时间戳可读化", w.fmt_reset(1789284070224) == _exp.strftime("%Y-%m-%d %H:%M:%S"),
      w.fmt_reset(1789284070224))

# T11 subagent rollout 文件被文件名过滤
sa = os.path.join(ROLL, "model-io-sess_subagent_agent_synth.jsonl")
append_line(sa, mk("rs", TEXT))
found = [p for p, _s in w.scan_rollout_files()]
check("T11 subagent rollout 文件排除", sa not in found and path in found)

# T12 普通正常响应不误触发
append_line(path, mk("r9", "这是正常回答,不含错误。"))
read_and_handle(t, path)
check("T12 正常回答不触发", len(SENT) == 8)

# T13(P1-2 新语义) 发送失败(如归位宁可不发)时不登记窗口,同窗口后续报错仍可重试
real_trigger = w.trigger_resume
w.trigger_resume = lambda reason, evt, a: False   # 模拟归位失败/注入失败
TEXT4 = TEXT.replace("1789284070224", "1792000000000")
append_line(path, mk("r10", TEXT4))
read_and_handle(t, path)
n_fail_logged = nlog("发送未完成,不计入熔断")
w.trigger_resume = real_trigger                    # 恢复:模拟后续恢复可用
append_line(path, mk("r11", TEXT4))                # 同窗口新报错
read_and_handle(t, path)
retried = (r for r in SENT if r == "sess_synthetic")
check("T13 发送失败不烧窗口→后续报错可重试", n_fail_logged == 1 and len(SENT) == 9)

# ---- T14-T21:broad 全量接管 / 黑名单 / 熔断 / B1 结构判别 / B2 判官 / 停滞 ----
w.TRIGGER_MODE = "broad"
w.EXCLUDE_ERROR_CAUSES = ("model_request_cancelled", "model_rate_limited",
                          "provider_not_configured", "MEDIA_BUDGET_CURRENT_IMAGE_TOO_LARGE")
w.EXCLUDE_ERROR_CODES = ("TURN_CANCELLED",)

EV = []
w.trigger_resume = lambda reason, evt, a: (EV.append((reason, evt.get("sessionId"))), True)[1]

def evfail(tid, cause, err_code="UNKNOWN_ERROR", sess="sess_evt"):
    return {"event": "turn.failed", "sessionId": sess, "turnId": tid,
            "error": {"code": err_code, "cause": {"code": cause}}}

# T14 broad:未知新错误原因默认触发(全量接管核心语义)
st_ev = w.WatchState()
w.handle_event_failure(evfail("t_u1", "some_brand_new_error"), st_ev, Args)
check("T14 broad 未知原因触发", len(EV) == 1 and EV[0][0] == "some_brand_new_error")

# T15 黑名单:已知"不发"类全部静默(取消两种形态/限流/配置缺失/媒体超限)
n0 = len(EV)
w.handle_event_failure(evfail("t_r1", "model_rate_limited"), st_ev, Args)
w.handle_event_failure(evfail("t_c1", "model_request_cancelled"), st_ev, Args)
w.handle_event_failure(evfail("t_p1", "provider_not_configured"), st_ev, Args)
w.handle_event_failure(evfail("t_m1", "MEDIA_BUDGET_CURRENT_IMAGE_TOO_LARGE"), st_ev, Args)
w.handle_event_failure({"event": "turn.failed", "sessionId": "sess_evt", "turnId": "t_c2",
                        "error": {"code": "TURN_CANCELLED"}}, st_ev, Args)
check("T15 黑名单五形态全排除", len(EV) == n0)

# T16 whitelist 回滚模式:未知原因不触发,老白名单照旧
w.TRIGGER_MODE = "whitelist"
st_wl = w.WatchState()
n0 = len(EV)
w.handle_event_failure(evfail("t_w1", "some_brand_new_error"), st_wl, Args)
check("T16a whitelist 未知原因不触发", len(EV) == n0)
w.handle_event_failure(evfail("t_w2", "model_request_failed"), st_wl, Args)
check("T16b whitelist 老白名单照旧", len(EV) == n0 + 1)
w.TRIGGER_MODE = "broad"

# T17 熔断:同会话连续 2 次自动发送后抑制,turn.completed 复位
w.CIRCUIT_BREAKER_LIMIT = 2
st_cb = w.WatchState()
n0 = len(EV)
w.handle_event_failure(evfail("t_b1", "mystery_a"), st_cb, Args)
w.handle_event_failure(evfail("t_b2", "mystery_a"), st_cb, Args)
w.handle_event_failure(evfail("t_b3", "mystery_a"), st_cb, Args)   # 应被熔断抑制
sent_cb = len(EV) - n0
w.track_turn_liveness({"event": "turn.completed", "sessionId": "sess_evt"}, st_cb)
w.handle_event_failure(evfail("t_b4", "mystery_a"), st_cb, Args)   # 复位后再发
check("T17 熔断 2 次后抑制→completed 复位", sent_cb == 2 and len(EV) == n0 + 3,
      "sends=%d" % sent_cb)
w.CIRCUIT_BREAKER_LIMIT = 99

# T18 B1 结构判别:provider 无关的 responseId 形态 / 错误信封文本
B1_TEXT = '[bridge-x error 429: {"message":"slow down"}]'
rec_rid = {"requestId": "q_b1a", "sessionId": "sess_synthetic",
           "response": {"responseId": "bridge-x-error-abc", "text": B1_TEXT}}
rec_txt = {"requestId": "q_b1b", "sessionId": "sess_synthetic",
           "response": {"responseId": "msg_ok123", "text": "[other-provider error 502: bad]"}}
check("T18a B1 responseId 形态命中", w.classify_modelio(rec_rid)[0].startswith("错误报文(B1"))
check("T18b B1 信封文本命中", w.classify_modelio(rec_txt)[0].startswith("错误报文(B1"))
prose = {"requestId": "q_p1", "sessionId": "sess_synthetic",
         "response": {"responseId": "msg_ok", "text": "正常回答里讨论 error 关键词"}}
empty_rec = {"requestId": "q_e1", "sessionId": "sess_synthetic",
             "response": {"responseId": None, "text": None}}
check("T18c 提到error的正常回答不误报", w.classify_modelio(prose, allow_judge=False) is None)
check("T18d 空响应记录不判错", w.classify_modelio(empty_rec) is None)

# T19 B1 防刷屏:无 reset 时间戳 → 文本哈希兜底,同窗口只发一次
st_b1 = w.WatchState()
n0 = len(EV)
w.handle_quota_record(rec_rid, "sess_synthetic", st_b1, Args)
w.handle_quota_record({"requestId": "q_b1c", "sessionId": "sess_synthetic",
                       "response": {"responseId": "bridge-x-error-abc", "text": B1_TEXT}},
                      "sess_synthetic", st_b1, Args)
check("T19 B1 无窗口抑制:两条报错各发一次", len(EV) == n0 + 2)

# T20 B2 判官(mock):语义区分"错误报文"与"讨论错误的正常回答"
real_judge = w.judge_text
w.JUDGE_ENABLED = True
w.JUDGE_MODEL = "mock-model"
prose2 = {"requestId": "q_j1", "sessionId": "sess_synthetic",
          "response": {"responseId": "msg_ok2", "text": "回复里提到 error: 上一轮报错已修复"}}
w.judge_text = lambda t: "error"
hit = w.classify_modelio(prose2)
check("T20a 判官判错→命中", hit is not None and hit[0].startswith("错误报文(LLM"))
n0 = len(EV)
w.handle_quota_record(dict(prose2), "sess_synthetic", w.WatchState(), Args)
check("T20b 判官通路触发发送", len(EV) == n0 + 1)
w.judge_text = lambda t: "ok"
check("T20c 判官判正常→不触发", w.classify_modelio(prose2) is None)
w.judge_text = real_judge
w.JUDGE_ENABLED = False
w.JUDGE_MODEL = ""

# T21 停滞看门狗:超时触发一次 / 同 turn 不叠加 / completed 复位熔断
st_st = w.WatchState()
w.track_turn_liveness({"event": "turn.started", "sessionId": "sess_evt", "turnId": "t_s1"}, st_st)
n0 = len(EV)
w.check_stalls(st_st, Args)                                   # 刚开始,不触发
st_st.open_turns["sess_evt"]["last"] = time.monotonic() - 10_000
w.check_stalls(st_st, Args)                                   # 超时 → 触发一次
fired = len(EV) - n0
w.check_stalls(st_st, Args)                                   # open turn 已弹出,不重复
check("T21a 停滞超时触发且每 turn 一次",
      fired == 1 and len(EV) == n0 + 1 and EV[-1][0] == "stall", "fired=%d" % fired)
n0 = len(EV)
w.handle_event_failure(evfail("t_s1", "model_request_failed"), st_st, Args)
check("T21b 停滞已续命的 turn 不再叠加", len(EV) == n0)
st_cb2 = w.WatchState()
w.CIRCUIT_BREAKER_LIMIT = 1
w.handle_event_failure(evfail("t_x1", "mystery_b"), st_cb2, Args)
w.track_turn_liveness({"event": "turn.completed", "sessionId": "sess_evt"}, st_cb2)
w.handle_event_failure(evfail("t_x2", "mystery_b"), st_cb2, Args)
check("T21c completed 复位熔断", len(EV) == n0 + 2)
w.CIRCUIT_BREAKER_LIMIT = 99

# ---- T22-T25:覆盖审计 --audit 机制 ----
# T22 _audit_disposition 覆盖全部排除原因与触发判定
d_sub = w._audit_disposition({"sessionId": "sess_subagent_x", "error": {}})
d_can = w._audit_disposition({"sessionId": "sess_a", "error": {"code": "TURN_CANCELLED"}})
d_blk = w._audit_disposition({"event": "turn.failed", "sessionId": "sess_a",
                              "error": {"code": "UNKNOWN_ERROR", "cause": {"code": "model_rate_limited"}}})
d_trg = w._audit_disposition({"event": "turn.failed", "sessionId": "sess_a",
                              "error": {"code": "UNKNOWN_ERROR", "cause": {"code": "model_request_failed"}}})
check("T22a 审计处置:subagent 排除", d_sub == (False, "排除:subagent 会话"), str(d_sub))
check("T22b 审计处置:用户取消排除", d_can[1] == "排除:用户取消", str(d_can))
check("T22c 审计处置:黑名单带调整提示", d_blk[0] is False and "exclude_error_causes" in d_blk[1], str(d_blk))
check("T22d 审计处置:可触发判定", d_trg[0] is True and d_trg[1].startswith("触发("), str(d_trg))

# T23 watcher.log 动作解析
wl_syn = os.path.join(SCR, "syn-watcher.log")
with open(wl_syn, "w", encoding="utf-8") as f:
    f.write("2026-09-11 10:00:00 监视器启动 (轮询 1.0s, live, 触发: broad, 熔断: 2, 停滞: 关, 心跳: 30min)\n")
    f.write("2026-09-11 10:01:00 已自动发送 '任务意外中断，请继续！' (connection, sess_abc-123)\n")
    f.write("2026-09-11 10:02:00 熔断中,跳过自动发送 (会话 sess_abc-123, 连续 5 次未恢复,等 turn.completed 复位)\n")
    f.write("2026-09-11 10:03:00 按「宁可不发」策略放弃本次发送 (quota, sess_def-456)\n")
    f.write("2026-09-11 10:04:00 心跳: 监视器运行中 (近 30 分钟无动作,空闲轮询)\n")
acts = w._parse_watcher_actions(wl_syn)
kinds = [a[1] for a in acts]
check("T23 watcher.log 动作解析(启动/发送/熔断/放弃/心跳)",
      kinds == ["启动", "发送", "熔断跳过", "归位放弃", "心跳"]
      and acts[1][2] == "sess_abc-123", str(kinds))

# T24 UTC→本地时间换算
naive = w._local_naive("2026-09-11T01:31:31.616Z")
expect = (datetime.datetime(2026, 9, 11, 1, 31, 31, 616000, tzinfo=datetime.timezone.utc)
          .astimezone().replace(tzinfo=None))
check("T24 UTC→本地换算", naive == expect, "%s" % naive)

# T25 端到端:合成日志上审计出"漏处理/悬空/黑名单提示"
syn_log_dir = os.path.join(SCR, "syn-log")
shutil.rmtree(syn_log_dir, ignore_errors=True)
os.makedirs(syn_log_dir)
ROLL2 = os.path.join(SCR, "syn-rollout")
shutil.rmtree(ROLL2, ignore_errors=True)
os.makedirs(ROLL2)
now_utc = datetime.datetime.now(datetime.timezone.utc)
def iso_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
ev_miss = {"timestamp": iso_utc(now_utc - datetime.timedelta(hours=3)), "event": "turn.failed",
           "sessionId": "sess_miss-1", "turnId": "turn_miss",
           "error": {"code": "UNKNOWN_ERROR", "message": "Turn execution failed",
                     "cause": {"code": "model_request_failed"}}}
ev_blk = {"timestamp": iso_utc(now_utc - datetime.timedelta(hours=2)), "event": "turn.failed",
          "sessionId": "sess_blk-1", "turnId": "turn_blk",
          "error": {"code": "UNKNOWN_ERROR", "message": "Turn execution failed",
                    "cause": {"code": "model_rate_limited"}}}
ev_st = {"timestamp": iso_utc(now_utc - datetime.timedelta(hours=2)), "event": "turn.started",
         "sessionId": "sess_dang-1", "turnId": "turn_dangling",
         "error": None}
ev_inflight = {"timestamp": iso_utc(now_utc - datetime.timedelta(seconds=30)), "event": "turn.started",
               "sessionId": "sess_fly-1", "turnId": "turn_inflight", "error": None}
day = (now_utc + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")  # 本地日期文件名
with open(os.path.join(syn_log_dir, "zcode-%s.jsonl" % day), "w", encoding="utf-8") as f:
    for ev in (ev_miss, ev_blk, ev_st, ev_inflight):
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

old_log_dir, old_log_file, old_roll = w.LOG_DIR, w.LOG_FILE, w.ROLLOUT_DIR
w.LOG_DIR = syn_log_dir
w.LOG_FILE = wl_syn
w.ROLLOUT_DIR = ROLL2
class AuditArgs(object):
    days = 7
rep = w.audit_mode(AuditArgs())
w.LOG_DIR, w.LOG_FILE, w.ROLLOUT_DIR = old_log_dir, old_log_file, old_roll
m_miss = re.search(r"未处理\(监视器未运行/无响应\) (\d+)", rep)
m_real = re.search(r"真悬空候选\(超过停滞阈值 \d+s\): (\d+)", rep)
m_fly = re.search(r"在飞\(未超阈值,正常\): (\d+)", rep)
check("T25a 审计:漏处理 1(合成)", m_miss and m_miss.group(1) == "1", m_miss.group(0) if m_miss else "无匹配")
check("T25b 审计:真悬空 1 + 在飞 1", m_real and m_real.group(1) == "1"
      and m_fly and m_fly.group(1) == "1",
      "%s | %s" % (m_real.group(0) if m_real else "无", m_fly.group(0) if m_fly else "无"))
check("T25c 审计:黑名单签名出现在报告且给出调整提示",
      "model_rate_limited" in rep and "exclude_error_causes" in rep)
check("T25d 审计:纯只读(未调用 trigger_resume 发送)",
      "已自动发送" not in rep)

# T25e 审计"运行中却无处理记录"不得误报:事件与"之后才发生的启动"落在同一匹配窗口时,
# 只凭"窗口内有动作"会报假的「疑似处理链路问题」——判据必须是"事件时刻有没有存活证据,
# 且其后没有被监视器退出掐断"(2026-10-12 实盘误报:12:19:28 事件 + 12:20:17 启动)。
syn2_dir = os.path.join(SCR, "syn-log-2")
shutil.rmtree(syn2_dir, ignore_errors=True)
os.makedirs(syn2_dir)
now2 = datetime.datetime.now()
ev_pre = {"timestamp": iso_utc((now2 - datetime.timedelta(minutes=60))
                               .astimezone(datetime.timezone.utc)),
          "event": "turn.failed", "sessionId": "sess_prestart-1", "turnId": "turn_pre",
          "error": {"code": "UNKNOWN_ERROR", "message": "Turn execution failed",
                    "cause": {"code": "model_request_failed"}}}
with open(os.path.join(syn2_dir, "zcode-%s.jsonl" % now2.strftime("%Y-%m-%d")),
          "w", encoding="utf-8") as f:
    f.write(json.dumps(ev_pre, ensure_ascii=False) + "\n")
wl2 = wl_syn + ".2"
with open(wl2, "w", encoding="utf-8") as f:
    f.write((now2 - datetime.timedelta(minutes=120)).strftime("%Y-%m-%d %H:%M:%S")
            + " 监视器启动 (轮询 1.0s, live, 触发: broad)\n")
    f.write((now2 - datetime.timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")
            + " 心跳: 监视器运行中 (近 30 分钟无动作,空闲轮询)\n")
    f.write((now2 - datetime.timedelta(minutes=70)).strftime("%Y-%m-%d %H:%M:%S")
            + " ⚠ 监视器退出: 控制台窗口被关闭(有人关了黑窗口) (PID 999)\n")
    f.write((now2 - datetime.timedelta(minutes=59, seconds=11)).strftime("%Y-%m-%d %H:%M:%S")
            + " 监视器启动 (轮询 1.0s, live, 触发: broad)\n")
w.LOG_DIR, w.LOG_FILE = syn2_dir, wl2
rep2 = w.audit_mode(AuditArgs())
w.LOG_DIR, w.LOG_FILE = old_log_dir, old_log_file
m_un2 = re.search(r"未处理\(监视器未运行/无响应\) (\d+)", rep2)
m_anom2 = re.search(r"运行中-无处理记录\(需排查\) (\d+)", rep2)
check("T25e 事件后 49s 才启动不得误报「运行中漏处理」(启动前有退出=当时没在跑)",
      m_un2 and m_un2.group(1) == "1" and m_anom2 is None
      and "疑似处理链路问题" not in rep2,
      "%s | anomaly=%s" % (m_un2.group(0) if m_un2 else "无",
                           m_anom2.group(0) if m_anom2 else "无(正确)"))
shutil.rmtree(syn2_dir, ignore_errors=True)
try:
    os.remove(wl2)
except OSError:
    pass

# ---- T26(2026-09-11 用户核心诉求):事件驱动节奏——"中断→发→又中断→再发" ----
# 用户原话:发 5 条指的是"第一次中断后,又报中断继续发一条,再报中断再发一条",
# 而不是"固定冷却时间到了就发"。此用例直接验证该语义(全程零时间等待)。
w.CIRCUIT_BREAKER_LIMIT = 5
w.TRIGGER_MODE = "broad"
EV2 = []
w.trigger_resume = lambda reason, evt, a: (EV2.append(evt.get("turnId")), True)[1]
st26 = w.WatchState()

def ev_conn(idx, sess="sess_26"):
    return {"event": "turn.failed", "sessionId": sess, "turnId": "t26_%d" % idx,
            "error": {"code": "UNKNOWN_ERROR", "message": "Turn execution failed",
                      "cause": {"code": "model_request_failed"}}}

for i in range(5):
    w.handle_event_failure(ev_conn(i), st26, Args)
check("T26a 连续 5 次独立中断→每条各发一次(零等待)",
      len(EV2) == 5 and EV2 == ["t26_%d" % i for i in range(5)], "sends=%d" % len(EV2))

w.handle_event_failure(ev_conn(5), st26, Args)
check("T26b 第 6 次被熔断挡住", len(EV2) == 5)

w.track_turn_liveness({"event": "turn.completed", "sessionId": "sess_26"}, st26)
w.handle_event_failure(ev_conn(6), st26, Args)
check("T26c 恢复后再中断→立即可发(计数已复位)", len(EV2) == 6)

w.handle_event_failure(ev_conn(7, sess="sess_26b"), st26, Args)
check("T26d 另一会话独立计数(不被连坐)", len(EV2) == 7)

before = len(EV2)
w.handle_event_failure(ev_conn(0), st26, Args)
check("T26e 同 turnId 重复投递不重发", len(EV2) == before)

w.CIRCUIT_BREAKER_LIMIT = 99   # 还原,避免影响后续用例

# ---- T28(2026-09-11 用户追问):用户主动取消/暂停 在任何情况下都不发 ----
# 用户问"无论什么问题都试 5 遍,有没有排除用户自己选择的暂停?"——此用例把该边界钉死。
EV28 = []
w.trigger_resume = lambda reason, evt, a: (EV28.append(reason), True)[1]
CASES_CANCEL = [
    {"event": "turn.failed", "sessionId": "s28a", "turnId": "c_a",
     "error": {"code": "TURN_CANCELLED", "message": "Turn was cancelled.",
               "cause": {"code": "model_request_cancelled"}}},
    {"event": "turn.failed", "sessionId": "s28b", "turnId": "c_b",
     "error": {"code": "TURN_CANCELLED", "message": "Turn was cancelled."}},
    {"event": "turn.failed", "sessionId": "s28c", "turnId": "c_c",
     "error": {"code": "TURN_CANCELLED", "message": "Turn was cancelled.", "cause": None}},
    {"event": "turn.failed", "sessionId": "s28d", "turnId": "c_d",
     "error": {"code": "TURN_CANCELLED", "message": "Turn was cancelled.",
               "cause": {"code": "model_request_cancelled", "context": {"reason": "user_stop"}}}},
    {"event": "turn.failed", "sessionId": "s28e", "turnId": "c_e",
     "error": {"code": "TURN_CANCELLED", "message": "Turn was cancelled.",
               "cause": {"code": "model_request_failed"}}},   # cause 像白名单码,但 code 是取消
]
st28 = w.WatchState()
for ev in CASES_CANCEL:
    w.handle_event_failure(dict(ev), st28, Args)
check("T28a 五种取消形态全部不触发(broad 模式)", len(EV28) == 0, "sends=%d" % len(EV28))

# 优先级:即便该会话处在"已连发 5 次"的熔断状态下,取消依然不发(优先级高于熔断)
st28b = w.WatchState()
w.CIRCUIT_BREAKER_LIMIT = 5
for i in range(5):
    w.handle_event_failure({"event": "turn.failed", "sessionId": "s28f", "turnId": "t28_%d" % i,
                            "error": {"code": "UNKNOWN_ERROR", "cause": {"code": "some_new_error"}}},
                           st28b, Args)
n_before_cancel = len(EV28)
w.handle_event_failure({"event": "turn.failed", "sessionId": "s28f", "turnId": "c_after_breaker",
                        "error": {"code": "TURN_CANCELLED", "cause": {"code": "model_request_cancelled"}}},
                       st28b, Args)
check("T28b 熔断状态下取消仍不发(取消优先级最高)", len(EV28) == n_before_cancel)
w.CIRCUIT_BREAKER_LIMIT = 99

# 反向对照:同结构的"非取消"失败应当照发(证明排除范围没有过度扩大)
n0 = len(EV28)
w.handle_event_failure({"event": "turn.failed", "sessionId": "s28g", "turnId": "t28_ok",
                        "error": {"code": "UNKNOWN_ERROR", "cause": {"code": "model_request_failed"}}},
                       st28b, Args)
check("T28c 对照:非取消的同类失败照发", len(EV28) == n0 + 1)

# ---- T27(2026-09-11 用户核心诉求):额度报错也重试满 5 次 ----
# 用户原话:"额度问题背后可能是供应商路由问题,重试几遍往往就成功,无论什么问题中断都想试 5 遍"
# 背景:旧版按"同额度重置窗口只发一次"抑制,导致额度类中断最多尝试 1 次,用不上熔断额度。
w.CIRCUIT_BREAKER_LIMIT = 5
EV3 = []
w.trigger_resume = lambda reason, evt, a: (EV3.append(evt.get("sessionId")), True)[1]
st27 = w.WatchState()
T27_TEXT = TEXT   # 复用 T2 的额度报错文本(同一 resetMs,验证窗口抑制已移除)

def q_rec(rid):
    return {"completedAt": "2026-09-11T10:00:00.000Z", "requestId": rid, "attempt": 1,
            "model": {"modelId": "monk"},
            "request": {"body": {"model": "monk", "messages": [{"role": "user", "content": "干活"}]}},
            "response": {"finishReason": "stop", "responseId": "qoder-error-" + rid,
                         "text": T27_TEXT, "toolCalls": [], "usage": {}},
            "sessionId": "sess_27", "turnId": "turn_27"}

# 同一额度窗口(同一 resetMs)的 5 条不同 requestId → 现在应各发一次
for i in range(5):
    w.handle_quota_record(q_rec("q27_%d" % i), "sess_27", st27, Args)
check("T27a 同一额度窗口 5 条报错→各发一次(不再只发 1 条)",
      len(EV3) == 5, "sends=%d" % len(EV3))

# 第 6 条 → 熔断到顶
w.handle_quota_record(q_rec("q27_5"), "sess_27", st27, Args)
check("T27b 额度路径同样受熔断封顶(第 6 条被挡)", len(EV3) == 5)

# turn.completed 复位后 → 又能发
w.track_turn_liveness({"event": "turn.completed", "sessionId": "sess_27"}, st27)
w.handle_quota_record(q_rec("q27_6"), "sess_27", st27, Args)
check("T27c 恢复后额度报错可再发", len(EV3) == 6)

# requestId 重复(同一次报错被重读)→ 仍只一次
before27 = len(EV3)
w.handle_quota_record(q_rec("q27_0"), "sess_27", st27, Args)
check("T27d 同 requestId 不重复发", len(EV3) == before27)

w.CIRCUIT_BREAKER_LIMIT = 99

# ---- T29(2026-09-12 P25):B0 特征串命中必须叠加结构门 ----
# 实测误报:上下文压缩摘要会把对话里讨论过的特征串原样带进 response.text
# (responseId 是普通 gen_ 形态,正文以 <analysis> 开头),旧 B0 特征串全命中
# 即触发,把"讨论错误的内容"当成"错误本身",连续打断健康会话。
w.trigger_resume = real_trigger
POISON = ("<analysis>\n对话摘要:项目此前讨论过如下额度报错形态:" + TEXT + " ,"
          "后续恢复正常。")   # 特征串三连命中,但正文是摘要散文
poison_rec = {"requestId": "q_p25a", "sessionId": "sess_p25",
              "response": {"finishReason": "stop", "responseId": "gen_01M2POISON",
                           "text": POISON}}
check("T29a 特征串全命中但正文非信封→find_quota_hit 拒绝",
      w.find_quota_hit(poison_rec) is None)
check("T29b 同记录三级判别整体不命中(B1 也不救)", 
      w.classify_modelio(poison_rec, allow_judge=False) is None)
EV29 = []
w.trigger_resume = lambda reason, evt, a: (EV29.append(evt.get("sessionId")), True)[1]
w.handle_quota_record(poison_rec, "sess_p25", w.WatchState(), Args)
check("T29c 摘要形态记录不触发发送", len(EV29) == 0)
w.trigger_resume = real_trigger
env_rec = {"requestId": "q_p25c", "sessionId": "sess_p25",
           "response": {"finishReason": "stop", "responseId": "msg_ok123", "text": TEXT}}
hit29 = w.classify_modelio(env_rec, allow_judge=False)
check("T29d 信封正文+普通 responseId 仍命中额度报错",
      hit29 is not None and hit29[0] == "额度报错")


# ---- T30(2026-10-10):送达核验 + 有界重试 + 禁止盲注入 ----
# 事故现场(私有仓复盘 P27):一次 cause=model_request_failed / providerCode=request_timeout /
# retryable=false 的 turn.failed 被判为 connection 并"发送成功",但注入整批落空——输入框当时不在
# 无障碍树里(会话切换/报错态),而 inject_text 的成功判据只是 PostMessage 入队;该会话停摆 7h48m
# 直到人工补发。本组只测编排(mock 窗口/注入/核验),不碰真窗口、不发送。
# 注:本文件的 w.trigger_resume 从第 49 行起就被换成 mock 了,故这里重新加载一份干净模块来测真编排。
_spec30 = importlib.util.spec_from_file_location(
    "zrw_t30", os.path.join(PROJ, "zcode_resume_watcher.py"))
w30 = importlib.util.module_from_spec(_spec30)
_spec30.loader.exec_module(w30)
LOG30 = []
w30.log = lambda msg: LOG30.append(msg)
w30.LOG_FILE = os.path.join(SCR, "t30-exit.log")   # 退出取证直接写文件,重定向到 scratch
open(w30.LOG_FILE, "w").close()                     # 清空:上一轮跑挂可能留下旧遗言,计数必须从零开始
w30.load_config()                    # 新键必须能从 config.json 落到模块常量
check("T30a 新配置键生效(deliver_check_enabled/timeout/resume_max_attempts)",
      w30.DELIVER_CHECK_ENABLED is True and w30.DELIVER_CHECK_TIMEOUT_SECONDS == 8.0
      and w30.RESUME_MAX_ATTEMPTS == 3,
      "timeout=%s attempts=%s" % (w30.DELIVER_CHECK_TIMEOUT_SECONDS, w30.RESUME_MAX_ATTEMPTS))

w30.IS_MACOS = False                # T30-T33 锁定的是 Windows 编排分支(注入/核验/重试);
                                    # 三平台 CI 都要跑这条,别让它落到 _trigger_resume_macos(osascript)
w30.NAVIGATE_ENABLED = False         # 绕开真实归位链(UIA / ps1)
w30.find_zcode_hwnd = lambda: 1
w30.DELIVER_CHECK_ENABLED = True
w30.DELIVER_CHECK_TIMEOUT_SECONDS = 0.0
w30.RESUME_MAX_ATTEMPTS = 3
w30.RESUME_RETRY_BACKOFF_SECONDS = 0.0
real_inject30 = w30.inject_text       # 供 T30i/T30j 直接测真注入链
real_focus30 = w30.uia_focus_input    # T30i 会把 uia_focus_input 换成 mock,这里留真函数给 T32g
real_wait30 = w30.wait_delivery       # 同上:t30_run 会把 wait_delivery 换成 mock,T33f 要用真函数

class LiveArgs(object):
    dry_run = False
    debug = False

ATT30 = []
def t30_run(verdicts, inject_results=(True,), evt=None):
    """跑一次真 trigger_resume;verdicts=逐次核验结果,inject_results=逐次注入结果(末尾沿用)。"""
    del ATT30[:]
    vi, ii = iter(verdicts), iter(inject_results)
    mark = len(LOG30)
    w30.inject_text = lambda hwnd, text, focus_done=False: (ATT30.append(text),
                                                            next(ii, inject_results[-1]))[1]
    w30.wait_delivery = lambda sess, since_ms: next(vi, verdicts[-1])
    r = w30.trigger_resume("connection", evt or {"sessionId": "sess_t30"}, LiveArgs)
    return r, LOG30[mark:]

r, lg = t30_run([True])
check("T30b 核验送达→返回 True、只注入一次、记「确认送达」",
      r is True and len(ATT30) == 1 and any("确认送达" in l for l in lg),
      "r=%s n=%d logs=%s" % (r, len(ATT30), lg))

r, lg = t30_run([False, True])
check("T30c 首次未送达→重试一次即送达(注入两次)",
      r is True and len(ATT30) == 2 and any("确认送达" in l for l in lg)
      and any("未收到" in l for l in lg))

r, lg = t30_run([False, False, False])
check("T30d 三次仍未送达→返回 False + 显式「未确认送达」,且无假成功日志",
      r is False and len(ATT30) == 3 and any("未确认送达" in l for l in lg)
      and not any("已自动发送" in l for l in lg))

r, lg = t30_run([False, False, False], inject_results=(False,))
check("T30e 注入未执行(输入框未就绪)→不记成功,重试耗尽即人工介入",
      r is False and any("注入未执行" in l for l in lg)
      and any("未确认送达" in l for l in lg))

r, lg = t30_run([None])
check("T30f 库读不到→退回旧语义(记「未核验」,不因核验能力缺失而停摆)",
      r is True and any("未核验" in l for l in lg) and any("已自动发送" in l for l in lg))

r, lg = t30_run([True])
check("T30g 送达路径只写一行日志", len(lg) == 1 and "确认送达" in lg[0])

# 归位遇草稿(退出码 6)→ 不许覆盖,但要有界重试(草稿可能是误读/其它面板输入框,也可能是上一轮残留;
# 重试只重读、不发送);识别不出聊天输入框(退出码 7)→ 同样不盲打
w30.NAVIGATE_ENABLED = True
NAV30 = []
del ATT30[:]
w30.navigate_to_session = lambda hwnd, target, sess: (NAV30.append(6), (False, False, 6))[1]
w30.inject_text = lambda hwnd, text, focus_done=False: (ATT30.append(text), True)[1]
w30.wait_delivery = lambda sess, since_ms: True
mark = len(LOG30)
r = w30.trigger_resume("connection", {"sessionId": "sess_t30"}, LiveArgs)
lg = LOG30[mark:]
check("T30h 输入框有草稿(退出码6)→重试到上限、零注入、日志点明不覆盖",
      r is False and len(NAV30) == 3 and len(ATT30) == 0
      and any("已尝试 3 次" in l for l in lg) and any("草稿" in l for l in lg))

NAV30b = []
del ATT30[:]
w30.navigate_to_session = lambda hwnd, target, sess: (NAV30b.append(7), (False, False, 7))[1]
mark = len(LOG30)
r = w30.trigger_resume("connection", {"sessionId": "sess_t30"}, LiveArgs)
lg = LOG30[mark:]
check("T30h2 输入框身份未识别(退出码7)→零注入、重试到上限、日志点明拒盲打字",
      r is False and len(NAV30b) == 3 and len(ATT30) == 0
      and any("拒绝盲打字" in l for l in lg) and any("已尝试 3 次" in l for l in lg))

# 草稿在第二次尝试时消失(上一次是误读/用户已清空/输入框刚挂载)→ 应继续发出并确认送达
NAV30c = []
del ATT30[:]
_seq = iter([(False, False, 6), (True, True, 0)])
w30.navigate_to_session = lambda hwnd, target, sess: (NAV30c.append(1), next(_seq, (True, True, 0)))[1]
mark = len(LOG30)
r = w30.trigger_resume("connection", {"sessionId": "sess_t30"}, LiveArgs)
lg = LOG30[mark:]
check("T30h3 首次草稿误读不再判死→第二次尝试照常送达",
      r is True and len(NAV30c) == 2 and len(ATT30) == 1
      and any("确认送达" in l for l in lg))

# inject_text:兜底聚焦失败 → 一个字符都不许打(2026-10-10 漏送的直接成因)
class FakeU32(object):
    def __init__(self):
        self.calls = 0
    def PostMessageW(self, *a):
        self.calls += 1
        return 1

w30.bring_to_front = lambda hwnd: True
w30.uia_focus_input = lambda: False
w30.user32 = FakeU32()
ok = real_inject30(1, w30.MESSAGE)
check("T30i 兜底聚焦失败→拒绝盲打字(PostMessage 0 次)", ok is False and w30.user32.calls == 0)

w30.uia_focus_input = lambda: True
w30._focused_child_hwnd = lambda hwnd: 42
w30.user32 = FakeU32()
ok = real_inject30(1, "ab", focus_done=True)
check("T30j focus_done=True→逐字符+回车(2 字符=2 CHAR+KEYDOWN+KEYUP)",
      ok is True and w30.user32.calls == 4, "calls=%d" % w30.user32.calls)

# 送达判据必须只认「user 角色 + text part」:续命文案会被工具记录/摘要原样引用
T30DB = os.path.join(SCR, "t30-db.sqlite")
if os.path.exists(T30DB):
    os.remove(T30DB)
con30 = sqlite3.connect(T30DB)
con30.executescript(
    "CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);"
    "CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
    "time_created INTEGER, data TEXT);"
    "CREATE TABLE session_input(id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, delivery TEXT, "
    "payload TEXT, admitted_sequence INTEGER, promoted_sequence INTEGER, promoted_message_id TEXT, "
    "status TEXT, status_reason TEXT, time_created INTEGER, time_updated INTEGER);")
now30 = int(time.time() * 1000)
quoted = json.dumps({"type": "tool", "input": {"command": "echo " + w30.MESSAGE}},
                    ensure_ascii=False)
con30.execute("INSERT INTO message VALUES('m1','sess_q',?,?)",
              (now30, json.dumps({"role": "assistant"}, ensure_ascii=False)))
con30.execute("INSERT INTO part VALUES('p1','m1','sess_q',?,?)", (now30, quoted))
con30.execute("INSERT INTO message VALUES('m2','sess_u',?,?)",
              (now30, json.dumps({"role": "user"}, ensure_ascii=False)))
con30.execute("INSERT INTO part VALUES('p2','m2','sess_u',?,?)",
              (now30, json.dumps({"type": "text", "text": w30.MESSAGE}, ensure_ascii=False)))
con30.commit()
con30.close()
w30.DB_PATH = T30DB
check("T30k 工具记录引用文案≠送达(只认 user)",
      w30._db_delivery_seen("sess_q", now30 - 1000) is False)
check("T30l 用户消息真含文案=送达", w30._db_delivery_seen("sess_u", now30 - 1000) is True)
w30.DB_PATH = os.path.join(SCR, "no-such-db.sqlite")
check("T30m 库不存在→核验返回 None(交回调用方降级)",
      w30._db_delivery_seen("sess_u", now30) is None)

# 审计词表:「未确认送达」必须能被 --audit 单独分类,而不是混进「已发送」
T30LOG = os.path.join(SCR, "t30-watcher.log")
with open(T30LOG, "w", encoding="utf-8") as fh:
    fh.write("2026-10-10 00:58:45 ⚠ 续命未确认送达:已尝试 3 次,末次原因「注入已投递但会话未收到」,"
             "需人工介入 (connection, sess_t30x)\n")
    fh.write("2026-10-10 01:02:00 已自动发送并确认送达 '%s' (connection, sess_t30y)\n"
             % w30.MESSAGE)
acts30 = w30._parse_watcher_actions(T30LOG)
kinds = [a[1] for a in acts30]
check("T30n 审计词表:未确认送达→送达存疑,确认送达→发送",
      kinds == ["送达存疑", "发送"], str(kinds))

# ---- T31(2026-10-10):退出取证(遗言)——长驻进程必须能说出自己的死因 ----
# 背景:当天第 4 次"静默死亡",查 Application 日志与 WER 均零条 python 记录(不是崩溃),
# 而 Windows 对"关窗口 / Ctrl+C / 注销"本来就不写系统日志 → 只能由进程临死前自己留一句话。
def t31_file_lines():
    try:
        with open(w30.LOG_FILE, encoding="utf-8") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


RET31 = w30.console_ctrl_handler(0)
lines31 = [l for l in t31_file_lines() if "监视器退出" in l]
check("T31a 控制台 Ctrl+C 事件→留下遗言且仍按默认终止(返回 False)",
      RET31 is False and len(lines31) == 1 and "Ctrl+C" in lines31[0],
      lines31[0][-46:] if lines31 else "无遗言")

w30.console_ctrl_handler(2)
lines31 = [l for l in t31_file_lines() if "监视器退出" in l]
check("T31b 关窗口事件→遗言点明「控制台窗口被关闭」(此前这条死法零痕迹)",
      len(lines31) == 2 and "窗口被关闭" in lines31[1], lines31[-1][-40:] if lines31 else "无")

w30.console_ctrl_handler(6)
lines31 = [l for l in t31_file_lines() if "监视器退出" in l]
check("T31c 关机/注销事件映射正确", "关机" in lines31[-1])

n_before = len(t31_file_lines())
w30.console_ctrl_handler(99)          # 未知事件码:不能崩,也要留痕
check("T31d 未知控制台事件不崩且留痕",
      len(t31_file_lines()) == n_before + 1 and "控制台事件 99" in t31_file_lines()[-1])

try:
    raise ValueError("t31-synthetic-boom")
except ValueError:
    w30._dump_traceback()
tb31 = "\n".join(t31_file_lines())
check("T31e 未捕获异常→traceback 整段落进日志(不只依赖控制台)",
      "t31-synthetic-boom" in tb31 and "Traceback" in tb31)

real_log30 = w30.LOG_FILE
w30.LOG_FILE = os.path.join(SCR, "no-such-dir-t31", "x.log")
try:
    w30._note_exit("T31 不可写路径")   # 不可写时静默,绝不抛异常(取证不能反噬主流程)
    ok31f = True
except Exception:
    ok31f = False
check("T31f 日志不可写时取证静默失败(不抛异常)", ok31f)
w30.LOG_FILE = real_log30

# 审计词表:遗言要有自己的分类,否则「事件窗口内监视器挂了」会被误报成"处理链路异常"
T31LOG = os.path.join(SCR, "t31-watcher.log")
with open(T31LOG, "w", encoding="utf-8") as fh:
    fh.write("2026-10-10 10:46:08 ⚠ 监视器退出: 控制台窗口被关闭(有人关了黑窗口) (PID 7072)\n")
acts31 = w30._parse_watcher_actions(T31LOG)
check("T31g 审计词表:遗言→「监视器退出」",
      len(acts31) == 1 and acts31[0][1] == "监视器退出", str([a[1] for a in acts31]))

# CLI 回归:--test-navigate 的解包口径(本轮 review 抓到 navigate_to_session 由 2 元组变 3 元组后
# 该命令会 ValueError 崩掉,已修;这条测试把 CLI 全链锁住)
_saved_argv31 = sys.argv
w30.CHECK_UPDATES = False
w30.find_zcode_hwnd = lambda: 12345
w30.resolve_session_target = lambda sess: {"title": "t31", "project": "p", "time_updated": 0}
NAV31 = []
w30.navigate_to_session = lambda hwnd, target, sess: (NAV31.append(sess), (True, True, 0))[1]
sys.argv = ["zcode_resume_watcher.py", "--test-navigate", "sess_t31"]
code31 = None
try:
    w30.main()
except SystemExit as e:
    code31 = e.code
except Exception as e:
    code31 = "EXC:%s" % type(e).__name__
finally:
    sys.argv = _saved_argv31
check("T31h --test-navigate 全链不炸(元组解包回归锁定)",
      code31 == 0 and NAV31 == ["sess_t31"], "exit=%s" % code31)

# ---- T32(2026-10-10):外部 review(第二轮)修复项全部锁死 ----
# ①队列态也算"收到"(会话忙时输入先进 session_input,message 表里迟迟没有 → 旧核验会误判未送达并重发);
# ②JSON 转义/畸形行都不许影响判读;③配置非法值(inf / 空正则 / 负数)回退默认;
# ④草稿(6)/身份不明(7)是硬否决,NAV_FAIL_FALLBACK_SEND=true 也不许绕过;⑤身份不匹配不重试;
# ⑥控制台回调不许 print(窗口关闭只给几秒宽限,print 可能被"快速编辑"选中卡住)。
T32DB = os.path.join(SCR, "t32-db.sqlite")
if os.path.exists(T32DB):
    os.remove(T32DB)
con32 = sqlite3.connect(T32DB)
con32.executescript(
    "CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);"
    "CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
    "time_created INTEGER, data TEXT);"
    "CREATE TABLE session_input(id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, delivery TEXT, "
    "payload TEXT, admitted_sequence INTEGER, promoted_sequence INTEGER, promoted_message_id TEXT, "
    "status TEXT, status_reason TEXT, time_created INTEGER, time_updated INTEGER);")
now32 = int(time.time() * 1000)
con32.execute("INSERT INTO session_input VALUES('q1','sess_q2','sendText','startNow',?,1,NULL,NULL,"
              "'admitted',NULL,?,?)",
              (json.dumps({"text": w30.MESSAGE}, ensure_ascii=False), now32, now32))
con32.execute("INSERT INTO session_input VALUES('q2','sess_other','sendText','startNow',?,2,NULL,NULL,"
              "'admitted',NULL,?,?)", (json.dumps({"text": "别的话"}, ensure_ascii=False), now32, now32))
con32.execute("INSERT INTO message VALUES('m3','sess_esc',?,?)",
              (now32, json.dumps({"role": "user"}, ensure_ascii=True)))
con32.execute("INSERT INTO part VALUES('p3','m3','sess_esc',?,?)",
              (now32, json.dumps({"type": "text", "text": w30.MESSAGE}, ensure_ascii=True)))
con32.execute("INSERT INTO message VALUES('m4','sess_bad',?,?)", (now32, "[1,2]"))
con32.execute("INSERT INTO part VALUES('p4','m4','sess_bad',?,?)", (now32, json.dumps([1, 2])))
con32.execute("INSERT INTO message VALUES('m5','sess_bad',?,?)", (now32, ""))
con32.execute("INSERT INTO part VALUES('p5','m5','sess_bad',?,?)", (now32, ""))
con32.commit()
con32.close()
w30.DB_PATH = T32DB
check("T32a 队列态(输入已入队、message 表尚无)也算收到→不再重发",
      w30._db_delivery_seen("sess_q2", now32 - 1000) is True)
check("T32b 队列里别人的话不影响判定", w30._db_delivery_seen("sess_other", now32 - 1000) is False)
check("T32c JSON 转义存法(\\uXXXX)仍能判出送达", w30._db_delivery_seen("sess_esc", now32 - 1000) is True)
check("T32d 畸形行(顶层非 dict/空串)不抛异常且判为未送达",
      w30._db_delivery_seen("sess_bad", now32 - 1000) is False)

T32CFG = os.path.join(SCR, "t32-config.json")
with open(T32CFG, "w", encoding="utf-8") as fh:
    json.dump({"deliver_check_timeout_seconds": float("inf"),
               "composer_class_regex": "",
               "resume_max_attempts": -5}, fh)
w30.load_config(T32CFG)
check("T32e 配置 inf / -5 / 空正则 → 全部回退默认(8s / 3 次 / min-h-10)",
      w30.DELIVER_CHECK_TIMEOUT_SECONDS == 8.0 and w30.RESUME_MAX_ATTEMPTS == 3
      and w30.COMPOSER_CLASS_REGEX == "min-h-10",
      "timeout=%s attempts=%s composer=%r" % (w30.DELIVER_CHECK_TIMEOUT_SECONDS,
                                              w30.RESUME_MAX_ATTEMPTS, w30.COMPOSER_CLASS_REGEX))
w30.load_config()

for _code in (6, 7):
    w30.NAV_FAIL_FALLBACK_SEND = True
    w30.DELIVER_CHECK_ENABLED = False      # 只看是否打字,绕过核验
    del ATT30[:]
    w30.navigate_to_session = lambda hwnd, target, sess, _c=_code: (False, False, _c)
    w30.inject_text = lambda hwnd, text, focus_done=False: (ATT30.append(text), True)[1]
    mark = len(LOG30)
    r = w30.trigger_resume("connection", {"sessionId": "sess_t30"}, LiveArgs)
    check("T32f 退出码 %d + fallback=true → 零注入(硬否决)" % _code,
          r is False and len(ATT30) == 0 and any("不发送" in l for l in LOG30[mark:]))
w30.NAV_FAIL_FALLBACK_SEND = False
w30.DELIVER_CHECK_ENABLED = True


class _FakeRun(object):
    def __init__(self, code):
        self.code = code
        self.calls = 0
    def __call__(self, *a, **kw):
        self.calls += 1
        class _R(object):
            pass
        r = _R()
        r.returncode = self.code
        return r


_real_sub32 = w30.subprocess.run
fake7 = _FakeRun(7)
w30.subprocess.run = fake7
ok32g = real_focus30()
w30.subprocess.run = _real_sub32
check("T32g 聚焦脚本退 7(身份不匹配)→只跑一次、直接放弃",
      ok32g is False and fake7.calls == 1, "calls=%d" % fake7.calls)

import builtins
_real_print32 = builtins.print
_before32h = len(t31_file_lines())


def _boom_print(*a, **kw):
    raise AssertionError("console handler must not print")


builtins.print = _boom_print
try:
    ret32h = w30.console_ctrl_handler(2)
    ok32h = True
except Exception:
    ok32h = False
finally:
    builtins.print = _real_print32
check("T32h 控制台回调不打印、只落文件(窗口关闭的几秒宽限内必达)",
      ok32h and ret32h is False and len(t31_file_lines()) == _before32h + 1)
for _f in (T32DB, T32CFG):
    try:
        os.remove(_f)
    except OSError:
        pass

# ---- T33(2026-10-10):第二轮外部评审(workflow 复核)确认的真 bug 全部锁死 ----
# ①队列 payload 的 text 非字符串 → 旧实现 `MESSAGE in 5` 抛 TypeError 掀翻主循环;
# ②config.json 顶层非对象 → cfg.get 抛 AttributeError,且发生在 run_watcher 的 try 之外;
# ③attempts 传 2.9/inf → 旧实现静默取整或 OverflowError;④正则配置传数字 → TypeError;
# ⑤事件 JSONL 顶层数组/标量被当事件 → 主循环 evt.get 崩。
T33CFG = os.path.join(SCR, "t33-config.json")
with open(T33CFG, "w", encoding="utf-8") as fh:
    json.dump({"resume_max_attempts": 2.9, "composer_class_regex": 123}, fh)
w30.load_config(T33CFG)
check("T33a 配置 attempts=2.9(非整数)+正则=123(非字符串)→回退默认,不再静默取整/崩",
      w30.RESUME_MAX_ATTEMPTS == 3 and w30.COMPOSER_CLASS_REGEX == "min-h-10",
      "attempts=%s composer=%r" % (w30.RESUME_MAX_ATTEMPTS, w30.COMPOSER_CLASS_REGEX))
with open(T33CFG, "w", encoding="utf-8") as fh:
    fh.write("[1, 2, 3]")
w30.load_config(T33CFG)
check("T33b config.json 顶层是数组→不崩、保持默认", w30.RESUME_MAX_ATTEMPTS == 3)
with open(T33CFG, "w", encoding="utf-8") as fh:
    json.dump({"resume_max_attempts": 1e999, "deliver_check_timeout_seconds": 12.5,
               "composer_class_regex": "abc-def", "deliver_check_enabled": False}, fh)
w30.load_config(T33CFG)
check("T33c inf 尝试次数→回退默认;合法非默认值确实生效(12.5s/abc-def/关核验)",
      w30.RESUME_MAX_ATTEMPTS == 3 and w30.DELIVER_CHECK_TIMEOUT_SECONDS == 12.5
      and w30.COMPOSER_CLASS_REGEX == "abc-def" and w30.DELIVER_CHECK_ENABLED is False,
      "attempts=%s timeout=%s composer=%r enabled=%s" % (
          w30.RESUME_MAX_ATTEMPTS, w30.DELIVER_CHECK_TIMEOUT_SECONDS,
          w30.COMPOSER_CLASS_REGEX, w30.DELIVER_CHECK_ENABLED))
w30.load_config()

# 队列行 text 非字符串/畸形:不许抛异常,也不许误判送达
T33DB = os.path.join(SCR, "t33-db.sqlite")
if os.path.exists(T33DB):
    os.remove(T33DB)
con33 = sqlite3.connect(T33DB)
con33.executescript(
    "CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);"
    "CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);"
    "CREATE TABLE session_input(id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, delivery TEXT, "
    "payload TEXT, admitted_sequence INTEGER, promoted_sequence INTEGER, promoted_message_id TEXT, "
    "status TEXT, status_reason TEXT, time_created INTEGER, time_updated INTEGER);")
now33 = int(time.time() * 1000)
for idx, bad in enumerate([json.dumps({"text": 5}), json.dumps({"text": [1, 2]}),
                           json.dumps({"text": None}), json.dumps([1, 2]), "not-json"]):
    con33.execute("INSERT INTO session_input VALUES(?,?, 'sendText','startNow',?,1,NULL,NULL,'admitted',NULL,?,?)",
                  ("b%d" % idx, "sess_badq", bad, now33, now33))
con33.commit()
con33.close()
w30.DB_PATH = T33DB
ok33d = True
try:
    r33d = w30._db_delivery_seen("sess_badq", now33 - 1000)
except Exception as e:
    ok33d = False
    r33d = "EXC:%s" % type(e).__name__
check("T33d 队列 payload 的 text 非字符串(数字/数组/null)/非对象 → 不抛异常且不误判送达",
      ok33d and r33d is False, "结果=%s" % r33d)

# 事件 JSONL 顶层非 dict:只接字典事件
t33 = w30.Tailer()
T33LOG2 = os.path.join(SCR, "t33-events.jsonl")
with open(T33LOG2, "w", encoding="utf-8") as fh:
    fh.write("[1, 2]" + chr(10)
             + json.dumps({"event": "turn.started", "sessionId": "sess_x"}) + chr(10)
             + "42" + chr(10))
t33.read_new(T33LOG2)                     # 首次打开从文件尾开始(冷启动不追溯)
t33.offset = 0; t33.partial = b""         # 手动拉回开头,模拟"读到这三行"
evts33 = t33.read_new(T33LOG2)
check("T33e Tailer 只把 JSON 对象当事件(数组/标量行被丢弃)",
      len(evts33) == 1 and isinstance(evts33[0], dict) and evts33[0].get("event") == "turn.started",
      "n=%d" % len(evts33))

# 真 wait_delivery(不用 mock):命中即返回 True;无命中等超时返回 False
w30.DB_PATH = T33DB
con33b = sqlite3.connect(T33DB)
con33b.execute("INSERT INTO message VALUES('m33','sess_hit',?,?)", (now33, json.dumps({"role": "user"})))
con33b.execute("INSERT INTO part VALUES('p33','m33','sess_hit',?,?)",
               (now33, json.dumps({"type": "text", "text": w30.MESSAGE})))
con33b.commit(); con33b.close()
t0 = time.monotonic()
hit33 = real_wait30("sess_hit", now33 - 1000)
dt_hit = time.monotonic() - t0
w30.DELIVER_CHECK_TIMEOUT_SECONDS = 0.6
t0 = time.monotonic()
miss33 = real_wait30("sess_miss", now33 - 1000)
dt_miss = time.monotonic() - t0
w30.DELIVER_CHECK_TIMEOUT_SECONDS = 0.0
check("T33f 真 wait_delivery 接通 SQL 与轮询:命中立刻 True、无命中等满超时才 False",
      hit33 is True and dt_hit < 0.5 and miss33 is False and 0.5 <= dt_miss < 3.0,
      "hit=%.2fs miss=%.2fs" % (dt_hit, dt_miss))

# 注册函数本身要真被调用(此前只测回调,没测注册);事件码 5(注销)也要覆盖
_k32calls = []


def _fake_sch(cb, add):
    _k32calls.append((cb, add))
    return 1


class _FakeK32(object):
    pass


before33 = len(t31_file_lines())
if w30.IS_WINDOWS:
    fakek = _FakeK32()
    fakek.SetConsoleCtrlHandler = _fake_sch  # 实例属性=普通函数:可挂 argtypes,与真实 ctypes 用法一致
    real_k32 = w30.kernel32
    w30.kernel32 = fakek
    reg33 = w30.install_console_ctrl_logger()
    w30.kernel32 = real_k32
    expect_reg33 = True
    kept33 = len(w30._CONSOLE_CTRL_CB) >= 1  # 回调被引用住(防 GC)
else:
    # 非 Windows:注册函数必须安全返回 False 且不触碰 kernel32(CI 三平台都要跑这个文件)
    reg33 = w30.install_console_ctrl_logger()
    expect_reg33 = False
    kept33 = True
w30.console_ctrl_handler(5)                  # 两个平台都覆盖事件码 5(注销)的映射与留痕
check("T33g 控制台回调注册(Win:真注册进 kernel32 / 非 Win:安全返回 False)+事件码 5 有遗言",
      reg33 is expect_reg33 and kept33 and len(t31_file_lines()) == before33 + 1
      and "注销" in t31_file_lines()[-1],
      "reg=%s calls=%d platform=%s" % (reg33, len(_k32calls), "win" if w30.IS_WINDOWS else "other"))

# 注入链断言到参数级:目标句柄 + 两个字符按序 + 回车按下/抬起
class _RecU32(object):
    def __init__(self):
        self.calls = []

    def PostMessageW(self, hwnd, msg, wparam, lparam):
        self.calls.append((hwnd, msg, wparam, lparam))
        return 1


w30.bring_to_front = lambda hwnd: True
w30.uia_focus_input = lambda: True
w30._focused_child_hwnd = lambda hwnd: 42
rec33 = _RecU32()
w30.user32 = rec33
ok33h = real_inject30(7, "ab", focus_done=True)
w30.user32 = real_user32_33 = None
seq33 = [(c[0], c[1], c[2]) for c in rec33.calls]
check("T33h 注入链参数级断言:同一句柄 2 个 WM_CHAR(按序)+回车按下/抬起",
      ok33h is True and seq33 == [(42, 0x0102, ord("a")), (42, 0x0102, ord("b")),
                                  (42, 0x0100, 0x0D), (42, 0x0101, 0x0D)],
      str(seq33))

# ps1 语法 + 纯 ASCII 闸门进套件(此前只有人工实测)
ps33 = None
_psh33 = "powershell" if w30.IS_WINDOWS else "pwsh"   # CI 的 ubuntu/macos runner 只有 pwsh
_ps_ok33 = None
_ev33 = ""
try:
    ps33 = subprocess.run([_psh33, "-NoProfile", "-Command",
    "$errs=$null; foreach($f in @('" + PROJ.replace(chr(92), "/") + "/uia_focus_input.ps1','"
    + PROJ.replace(chr(92), "/") + "/uia_navigate_input.ps1'))"
    "{[System.Management.Automation.Language.Parser]::ParseFile($f,[ref]$null,[ref]$errs)|Out-Null;"
    "$b=[System.IO.File]::ReadAllBytes($f); Write-Output ('x' + @($errs).Count + ' n' + @($b|Where-Object{$_ -gt 127}).Count)}"],
        capture_output=True, text=True, timeout=120)
    _out33 = ps33.stdout or ""
    _ps_ok33 = (_out33.count("x0 n0") == 2)
    _ev33 = _out33.strip()[:60]
except FileNotFoundError:
    _ps_ok33 = True
    _ev33 = "skip: 本机没有 %s(编码闸门只在有 PowerShell 的机器上真跑)" % _psh33
check("T33i 两个 ps1 语法零错误且保持纯 ASCII(编码纪律闸门)", _ps_ok33 is True, _ev33)

for _f in (T33DB, T33CFG, T33LOG2):
    try:
        os.remove(_f)
    except OSError:
        pass

# ---- T34(2026-10-10):macOS 触发链同样接入送达核验 + 有界重试 ----
# 同类漏洞:此前 mac_inject_text 返回真即记「已自动发送」,从不核验 —— 与 Windows 修掉的假成功同源。
# mac 原语(osascript/AX)在本机不可跑,故全部 mock;核验走**真** wait_delivery + 临时库。
T34DB = os.path.join(SCR, "t34-db.sqlite")
if os.path.exists(T34DB):
    os.remove(T34DB)
con34 = sqlite3.connect(T34DB)
con34.executescript(
    "CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);"
    "CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);"
    "CREATE TABLE session_input(id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, delivery TEXT, "
    "payload TEXT, admitted_sequence INTEGER, promoted_sequence INTEGER, promoted_message_id TEXT, "
    "status TEXT, status_reason TEXT, time_created INTEGER, time_updated INTEGER);")
now34 = int(time.time() * 1000)
con34.execute("INSERT INTO message VALUES('m34','sess_mac_ok',?,?)",
              (now34, json.dumps({"role": "user"})))
con34.execute("INSERT INTO part VALUES('p34','m34','sess_mac_ok',?,?)",
              (now34, json.dumps({"type": "text", "text": w30.MESSAGE})))
con34.commit()
con34.close()

_saved34 = (w30.IS_MACOS, w30.NAVIGATE_ENABLED, w30.DB_PATH,
            w30.DELIVER_CHECK_TIMEOUT_SECONDS, w30.RESUME_RETRY_BACKOFF_SECONDS)
w30.IS_MACOS = True                     # 让 trigger_resume 走 mac 分支(所有 mac 原语都被 mock)
w30.NAVIGATE_ENABLED = False            # 先测最小链:注入 + 核验
w30.DB_PATH = T34DB
w30.DELIVER_CHECK_TIMEOUT_SECONDS = 0.0
w30.RESUME_RETRY_BACKOFF_SECONDS = 0.0
w30.wait_delivery = real_wait30         # 显式用真核验(t30_run 留下的 mock 会假装送达)
MAC34 = []
w30.mac_inject_text = lambda text: (MAC34.append(text), True)[1]
w30.mac_navigate = lambda title: (True, False)

mark = len(LOG30)
r = w30.trigger_resume("connection", {"sessionId": "sess_mac_ok"}, LiveArgs)
check("T34a mac:注入后回库核验送达→True 且只注入一次、记「确认送达」",
      r is True and len(MAC34) == 1 and any("确认送达" in l for l in LOG30[mark:]))

del MAC34[:]
mark = len(LOG30)
r = w30.trigger_resume("connection", {"sessionId": "sess_mac_miss"}, LiveArgs)
check("T34b mac:未送达→重试到上限、显式「未确认送达」(不再记假成功)",
      r is False and len(MAC34) == 3 and any("未确认送达" in l for l in LOG30[mark:])
      and not any("已自动发送" in l for l in LOG30[mark:]))

# 归位分支:AX 可读但验证不到 → 宁可不发(零注入);AX 不可读 → 降级发送但同样核验
w30.NAVIGATE_ENABLED = True
w30.mac_navigate = lambda title: (False, False)
del MAC34[:]
r = w30.trigger_resume("connection", {"sessionId": "sess_mac_ok"}, LiveArgs)
check("T34c mac:归位已验证失败→宁可不发(零注入)", r is False and len(MAC34) == 0)

w30.mac_navigate = lambda title: (False, True)     # AX 整体不可读 → 降级当前会话
del MAC34[:]
mark = len(LOG30)
r = w30.trigger_resume("connection", {"sessionId": "sess_mac_ok"}, LiveArgs)
check("T34d mac:AX 不可读→仍降级发送,但同样要核验送达",
      r is True and len(MAC34) == 1 and any("AX 不可读" in l for l in LOG30[mark:])
      and any("确认送达" in l for l in LOG30[mark:]))

# 重试后成功:第一次未送达、第二次已送达(mock 核验序列)
w30.NAVIGATE_ENABLED = False
_seq34 = iter([False, True])
w30.wait_delivery = lambda sess, since_ms: next(_seq34, True)
del MAC34[:]
mark = len(LOG30)
r = w30.trigger_resume("connection", {"sessionId": "sess_mac_x"}, LiveArgs)
check("T34e mac:首次未送达→重试一次即送达(注入两次,与 Windows 同款语义)",
      r is True and len(MAC34) == 2 and any("确认送达" in l for l in LOG30[mark:]))

(w30.IS_MACOS, w30.NAVIGATE_ENABLED, w30.DB_PATH,
 w30.DELIVER_CHECK_TIMEOUT_SECONDS, w30.RESUME_RETRY_BACKOFF_SECONDS) = _saved34
w30.wait_delivery = real_wait30
try:
    os.remove(T34DB)
except OSError:
    pass

# ---- T35(2026-10-11):焦点落地核验 —— SetFocus 被静默忽略时绝不盲打字 ----
# 用户实测:续命文字被打进浏览器面板的**网址输入框**与**自由尺寸视口宽高框**。链路:身份判据
# 只保证"选谁",不保证 SetFocus 真把焦点落到它身上;而 PostMessage 的 WM_CHAR 跟着**真实焦点**
# 走。修法:两个 ps1 都在 SetFocus 后回读 AutomationElement.FocusedElement 核验落地(退 8),
# Python 侧把 8 与 6/7 同级硬否决,\-\-test-send 人工试用也留档。本组只测编排与静态闸门。
check("T35a 退出码 8 有中文释义(聚焦脚本/归位脚本两侧)",
      "焦点" in w30.FOCUS_VERDICTS.get(8, "") and "焦点" in w30.NAV_VERDICTS.get(8, ""),
      "focus=%r nav=%r" % (w30.FOCUS_VERDICTS.get(8), w30.NAV_VERDICTS.get(8)))

_saved35 = (w30.NAV_FAIL_FALLBACK_SEND, w30.DELIVER_CHECK_ENABLED, w30.RESUME_MAX_ATTEMPTS,
            w30.RESUME_RETRY_BACKOFF_SECONDS)
w30.NAV_FAIL_FALLBACK_SEND = True          # 故意打开回退:8 仍必须是硬否决
w30.DELIVER_CHECK_ENABLED = True           # 保持重试链(N 次失败才收口),注入为零与核验无关
w30.RESUME_MAX_ATTEMPTS = 3
w30.RESUME_RETRY_BACKOFF_SECONDS = 0.0
del ATT30[:]
w30.navigate_to_session = lambda hwnd, target, sess: (False, False, 8)
w30.inject_text = lambda hwnd, text, focus_done=False: (ATT30.append(text), True)[1]
mark = len(LOG30)
r = w30.trigger_resume("connection", {"sessionId": "sess_t35"}, LiveArgs)
lg = LOG30[mark:]
check("T35b 归位退 8(焦点没落地)+fallback=true → 零注入、重试到上限、日志点明拒盲打字",
      r is False and len(ATT30) == 0 and any("焦点未落到聊天输入框" in l for l in lg)
      and any("已尝试 3 次" in l for l in lg), str(lg[-1:]))

# 关键回归:focus_done=True(归位脚本已聚焦)也必须重新核验 —— 旧实现直接跳过聚焦,
# 焦点在谁身上就把 WM_CHAR 打给谁(正是"打进面板输入框"的那条路)
w30.bring_to_front = lambda hwnd: True
w30.uia_focus_input = lambda: False
fake35 = FakeU32()
w30.user32 = fake35
ok35c = real_inject30(1, "ab", focus_done=True)
check("T35c focus_done=True 仍先核验:核验不过→零注入", ok35c is False and fake35.calls == 0)

w30.uia_focus_input = lambda: True
rec35 = _RecU32()
w30.user32 = rec35
w30._focused_child_hwnd = lambda hwnd: 42
ok35d = real_inject30(1, "ab", focus_done=True)
check("T35d 核验通过→照常逐字符投递(2 CHAR+回车按下/抬起)",
      ok35d is True and len(rec35.calls) == 4, "calls=%d" % len(rec35.calls))


class _FakeRun35(object):
    def __init__(self, code, out=b"", err=b""):
        self.code = code
        self.out = out
        self.err = err
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1

        class _R(object):
            pass
        r = _R()
        r.returncode = self.code
        r.stdout = self.out
        r.stderr = self.err
        return r


_real_sub35 = w30.subprocess.run
fake35b = _FakeRun35(7, out=b"CANDIDATES not-composer total=3 visibleFocusable=2 "
                                 b"[y=858 cls=min-h-10 alt] [y=120 cls=h-8 rounded-full]")
w30.subprocess.run = fake35b
mark = len(LOG30)
ok35e = real_focus30()
w30.subprocess.run = _real_sub35
check("T35e 聚焦脚本失败→CANDIDATES 候选清单随日志落盘(拒绝可解释、只跑一次)",
      ok35e is False and fake35b.calls == 1 and any("CANDIDATES" in l for l in LOG30[mark:]),
      str(LOG30[mark:]))

# --test-send(人工试用)要留档:过去无论成功还是打偏都不写日志,试用结果事后无从复盘
_saved_argv35 = sys.argv
_saved_sleep35 = time.sleep
w30.CHECK_UPDATES = False
w30.find_zcode_hwnd = lambda: 12345
del ATT30[:]
w30.inject_text = lambda hwnd, text, focus_done=False: (ATT30.append(text), True)[1]
time.sleep = lambda s: None
sys.argv = ["zcode_resume_watcher.py", "--test-send"]
mark = len(LOG30)
code35 = "?"
try:
    w30.main()
    code35 = None
except SystemExit as e:
    code35 = e.code
finally:
    sys.argv = _saved_argv35
    time.sleep = _saved_sleep35
check("T35f --test-send 留档:开始与结果两行都进 watcher.log",
      code35 is None and len(ATT30) == 1
      and any("测试发送(人工触发)" in l for l in LOG30[mark:])
      and any("测试发送结果" in l for l in LOG30[mark:]), "exit=%s %s" % (code35, LOG30[mark:]))

# ps1 静态闸门:两个脚本必须真带落地核验与候选清单(防漏改/回退);语法与 ASCII 由 T33i 管
_ps35 = {}
for _f in ("uia_focus_input.ps1", "uia_navigate_input.ps1"):
    try:
        _txt = open(os.path.join(PROJ, _f), encoding="utf-8").read()
    except OSError:
        _txt = ""
    _ps35[_f] = ("FocusedElement" in _txt) and ("exit 8" in _txt) and ("CANDIDATES" in _txt)
check("T35g 两个 ps1 都带焦点落地核验 + 候选清单 + 退出码 8",
      all(_ps35.values()), str(_ps35))

w30.NAV_FAIL_FALLBACK_SEND, w30.DELIVER_CHECK_ENABLED, w30.RESUME_MAX_ATTEMPTS, \
    w30.RESUME_RETRY_BACKOFF_SECONDS = _saved35

# T30 组自清理:测试产物不留仓(gitignore 兜底,跑挂时也不会变成未跟踪噪音)
for _f in (T30DB, T30LOG, os.path.join(SCR, "t30-exit.log"), T31LOG):
    try:
        os.remove(_f)
    except OSError:
        pass


fails = [n for n, ok in PASS if not ok]
print("\n%d/%d PASS" % (len(PASS) - len(fails), len(PASS)))
if fails:
    print("FAILED:", fails)
    sys.exit(1)