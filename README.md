<div align="center">

# zcode-resume-watcher

**ZCode 断连自动续命监视器** — Keep your ZCode sessions alive

ZCode 桌面端的 Agent 任务因断连 / 空响应 / 限流 / provider 额度"假成功"而意外停止时，
自动把续命消息发回出错的会话——挂机跑任务不再卡死。

零第三方依赖 · 单文件 · 本地运行 · 无遥测

**Windows 全功能** · **macOS 实验版** · 纯 Python 标准库

</div>

---

## 为什么需要它

ZCode 里跑长任务时，两类失败会让任务悄悄死掉，人却未必在旁边盯着：

| 失败形态 | 表现 | 后果 |
|---|---|---|
| **显性失败** | ZCode 记录 `turn.failed`（网络断连、重试耗尽、空响应、限流等） | 任务停止，等人点"继续" |
| **隐形失败**（更阴） | 桥接/中转类 provider 把错误文本**当成正常回答返回**，ZCode 视角这一轮"完成"了 | 任务停止且**没有任何错误提示**，日志里也查不到 `turn.failed` |

本工具在后台监听 ZCode 的本地日志，检测到这两种失败后，**自动把「任务意外中断，请继续！」发回出错的会话**。同一个会话反复中断就反复续命（每会话上限 5 次，出现正常完成立即复位），额度类报错往往是中转路由抖动，多试一次常就能恢复。

## 功能特性

- **双通道检测**
  - 事件日志：捕获 `turn.failed` 显性失败（broad 模式，未知新错误类型也默认覆盖）；
  - 模型 IO 日志：捕获"假成功"隐形失败（特征串 + 错误信封结构双重判别，provider 无关，可选 LLM 判官兜底）。
- **全量触发 ≠ 什么都发**：两类情况永不打扰——①用户主动取消/暂停（优先级最高）②黑名单"重试无解"类；其余失败自动续命。
- **智能归位**（Windows）：发送前把界面切回出错的会话本身——sessionId 反查会话标题 → 侧栏定位点击 → 顶栏标题逐字验证 → 输入框有草稿则放弃（宁可不发，绝不错发到别的对话）。
- **覆盖审计** `--audit`：只读扫描历史日志，报告"哪些中断没被续命到"（区分监视器没在跑 vs 规则没覆盖），以及悬空 turn、新报错形态候选——扩面的日常入口。
- **更新检查** `--check-updates`：查询本仓库最新 Release 提示新版本（只提示不自动更新，可配置关闭）。
- **交互式架构页**：`architecture.html` 单文件直开，11 个可点击场景演示信号管线与守卫语义，含无头自检钩子。

## 安装

### Windows（全功能）

1. 从 [Releases](../../releases) 下载 zip 并解压（或 `git clone` 本仓库）；
2. 双击 **`启动自动续命.bat`** —— 不需要预装 Python：脚本会自动从 python.org 下载官方便携版到用户目录（不改注册表、不加 PATH，随时可整目录删除）；
3. 最小化黑色窗口即可，双击多次不会重复启动。

也可以用 `测试发送.bat` 验证注入链路（10 秒倒计时后向当前会话发一条测试消息）。

### macOS（实验版 v1）

```bash
git clone https://github.com/JJ-Yvain/zcode-resume-watcher.git
cd zcode-resume-watcher
python3 zcode_resume_watcher.py
```

**必须先授权**：系统设置 → 隐私与安全性 → **辅助功能**，勾选你的终端 / Python 运行环境（注入依赖 AppleScript 模拟按键）。

**macOS v1 范围**：检测与守卫与 Windows 完全同一套；注入为"激活 ZCode → 剪贴板粘贴 → 回车"（自动恢复剪贴板）。暂不支持智能归位与草稿检测，消息发往 ZCode 当前焦点会话——请保持出错会话在前台。

## 常用命令

```bash
python zcode_resume_watcher.py                      # 前台运行(同 bat)
python zcode_resume_watcher.py --dry-run            # 只记录将发送,不真正打字
python zcode_resume_watcher.py --test-send          # 立即测试发送一次(10 秒倒计时)
python zcode_resume_watcher.py --test-navigate <sessionId>   # 归位演练(仅 Windows)
python zcode_resume_watcher.py --audit --days 7     # 覆盖审计(只读)
python zcode_resume_watcher.py --check-updates      # 检查新版本
python zcode_resume_watcher.py --version            # 当前版本
```

## 配置

全部配置在 `config.json`（键都有中文注释语义，删掉该文件即回默认值），常用项：

| 键 | 默认 | 说明 |
|---|---|---|
| `message` | 任务意外中断，请继续！ | 续命文本，可自定义 |
| `circuit_breaker_limit` | 5 | 同会话连续自动发送上限；出现 `turn.completed` 即复位 |
| `trigger_mode` | broad | `whitelist` 回退到旧白名单模式 |
| `exclude_error_causes` / `exclude_error_codes` | 取消/无解类 | broad 模式黑名单 |
| `navigate_enabled` | true | 智能归位开关（Windows） |
| `navigate_fail_fallback_send` | false | 归位失败时是否仍发给当前对话 |
| `draft_check_enabled` | true | 输入框有未发送草稿时放弃发送 |
| `quota_enabled` | true | "假成功"隐形失败检测开关 |
| `check_updates` | true | 启动时查询最新版本（只提示） |
| `heartbeat_minutes` | 30 | 空闲心跳日志间隔（审计据此判定运行窗口） |

## 安全与隐私设计

- **用户取消永不打扰**：`model_request_cancelled` / `TURN_CANCELLED` 任何形态都排除，优先级高于一切；
- **宁可不发**：归位失败 / 输入框有草稿 / 注入未确认成功，一律不计入熔断、保留重试机会；
- **熔断兜底**：同一会话连续 5 次续命仍未恢复就闭嘴，出现正常完成立即复位；
- **数据不出本机**：只读本地日志与数据库，无遥测、无上报；更新检查仅匿名请求 GitHub API，可关。

## 已知限制

- ZCode 升级可能改动日志字段或 UI 结构（UIA 类名等）——`--audit` 的签名统计是早期预警，多数适配改 `config.json` 即可；
- ZCode 会定期清理 rollout 目录，历史 model-io 文件会消失（监听已兼容）；
- macOS v1 无智能归位/草稿检测（见上）；
- 监视器进程本身可能静默消失（无自动拉起，作者的有意取舍）——建议偶尔跑一次 `--audit` 兜底。

## 更新

- 启动时若配置 `check_updates: true`（默认开启），日志会提示是否有新版本；
- `git pull` 或重新下载 [Releases](../../releases) 里的 zip 覆盖即可；配置与日志文件不会被覆盖。

## 相关链接

- [architecture.html](architecture.html) — 交互式架构页（信号管线 / 11 场景 / 守卫语义）
- [tests/driver.py](tests/driver.py) — 57 项合成回归（`python tests/driver.py`，CI 在 Windows/macOS/Linux 三平台运行）

## License

[Apache-2.0](LICENSE)

> 本项目为社区工具，与 Z.ai / 智谱官方无关；"ZCode" 是其各自所有者的商标。

---

## English (TL;DR)

**zcode-resume-watcher** keeps ZCode desktop agent sessions alive. When a turn dies from network failures, empty responses, rate limits, or provider "fake success" errors (error text returned as a normal answer — invisible to ZCode's own error channel), the watcher detects it from local logs and automatically sends "the task was interrupted, please continue!" back to the affected session.

- **Detection**: dual-channel (event log + model-IO log), unknown error types covered by default, user-initiated cancellations and hopeless errors never triggered, per-session circuit breaker (5 sends, reset on success).
- **Smart re-focus** (Windows): resolves the session title from the local database, clicks the sidebar entry, verifies via the header title, and aborts if a draft is present (better to miss one than to send into the wrong conversation).
- **Fully local**: zero third-party dependencies, no telemetry; `--check-updates` only pings the GitHub API (disable via `check_updates: false`).
- **Platforms**: Windows full-featured; macOS experimental (AppleScript paste injection, Accessibility permission required, no re-focus/draft-check in v1).
- **Install**: grab the zip from [Releases](../../releases), run `启动自动续命.bat` (Windows; auto-bootstraps a portable Python) or `python3 zcode_resume_watcher.py` (macOS). See the Chinese sections above for full details.
