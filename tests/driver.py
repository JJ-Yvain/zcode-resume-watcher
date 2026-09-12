# -*- coding: utf-8 -*-
"""zcode-resume-watcher 合成回归测试(不发送、不碰真实日志)。
覆盖:额度"假成功"三级判别(T1-T13,T18-T20)、broad 全量接管/黑名单/熔断/停滞(T14-T17,T21)、
覆盖审计 --audit 机制(T22-T25)。
运行: python driver.py   → 全 PASS 退出码 0。"""
import datetime
import importlib.util
import json
import os
import re
import shutil
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows runner stdout 可能是 cp1252

SCR = os.path.dirname(os.path.abspath(__file__))
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


fails = [n for n, ok in PASS if not ok]
print("\n%d/%d PASS" % (len(PASS) - len(fails), len(PASS)))
if fails:
    print("FAILED:", fails)
    sys.exit(1)