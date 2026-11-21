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
- **智能归位**（Windows）：发送前把界面切回出错的会话本身——sessionId 反查会话标题 → 侧栏定位点击 → 顶栏标题逐字验证 → 输入框有草稿则放弃（宁可不发，绝不错发到别的对话）。聚焦后会**回读真实焦点核验落地**（`SetFocus` 可能被静默忽略，而按键跟着真实焦点走），焦点没落在聊天输入框上就拒发。
- **覆盖审计** `--audit`：只读扫描历史日志，报告"哪些中断没被续命到"（区分监视器没在跑 vs 规则没覆盖），以及悬空 turn、新报错形态候选——扩面的日常入口。
- **更新检查** `--check-updates`：查询本仓库最新 Release 提示新版本（只提示不自动更新，可配置关闭）。
- **桌面面板 + 托盘/菜单栏**（v1.2.0）：无黑窗启动；Windows 用 Edge `--app`、macOS 优先
  Chrome/Edge `--app`，展示 24h 续命记录与「检测→归位→注入→核验」四段链路，亮/暗双主题；
  关闭窗口不退出引擎，托盘/菜单栏可暂停/继续、测试发送、打开日志/配置、退出；
  窗口大小会被记住，下次打开或重启沿用。Windows 提供安装包/开始菜单/桌面快捷方式，macOS 提供
  `.app` 菜单栏应用构建链。
- **交互式架构页**：`architecture.html` 单文件直开，11 个可点击场景演示信号管线与守卫语义，含无头自检钩子。

## 安装

### Windows（全功能）

1. v1.2.0 尚未发 Release；安装包 `ZCodeResumeWatcher-<version>-Setup.exe` 从
   **GitHub Actions → Desktop Build run 的 Artifacts** 下载（或 `git clone` 本仓库，
   按 `packaging/README.md` 自行构建；v1.2.0 正式发版后可从 Releases 下载）；
2. 运行安装包：默认当前用户安装，向导可选目录/所有用户；创建开始菜单和**桌面快捷方式**
   （桌面可取消），可选登录启动；卸载走 Windows 设置；
3. 安装版内置 Python，无需预装。

源码/调试运行（需要 Python 3.12+）：

```bash
python ZCode续命面板.pyw                 # 桌面面板
python zcode_resume_watcher.py           # 控制台版
python zcode_resume_watcher.py --test-send
```

### 桌面面板（v1.2.0，推荐）

- 本地面板只绑定 `127.0.0.1` 随机端口，带每次启动随机 token；页面是单文件 HTML，无外部请求；
- 左侧近 24h 续命记录（全部 / 送达 / 异常 / 拦截），右侧一次恢复的四段链路与证据原文；
- 关闭应用窗 ≠ 退出引擎：Windows 引擎继续在托盘运行，macOS 在菜单栏；重新打开不会起第二个引擎；
- 暂停只拦自动发送，检测/日志照常；测试发送与自动续命共用发送锁，不会同时打字；
- 窗口大小自动记住；修改 `config.json` 后需重启应用。
- 依赖系统 Edge/Chrome 的 `--app` 模式；都没有时回落默认浏览器。面板 UI 是观察者/遥控器，
  不改变引擎的判定、去重、熔断、归位与「宁可不发」策略。

### macOS（实验版 v1）

```bash
git clone https://github.com/JJ-Yvain/zcode-resume-watcher.git
cd zcode-resume-watcher
python3 ZCode续命面板.pyw     # 源码入口（需要 Python 3.12+）
```

菜单栏 `.app` 构建（macOS）：

```bash
python3 -m pip install -r packaging/requirements-build.txt
bash packaging/build_macos.sh
```

产物是 `dist/ZCode 续命监视器.app` 和 DMG；`rumps`/PyObjC 只用于菜单栏，源码无这些依赖时回退无菜单栏信号循环。
**必须先授权**：系统设置 → 隐私与安全性 → **辅助功能**，勾选你的终端 / Python 运行环境（注入与智能归位都依赖 AppleScript 模拟按键/读取界面）。

**macOS 智能归位（v1.5，beta）**：与 Windows 同思路——从本地数据库反查出会话标题，在 ZCode 界面里找到该会话行并点击，验证顶栏标题后发送；**看不清界面（AX 不可读）时自动降级为发送到当前会话并记日志，看得清但找不到目标会话时宁可不发**。首次适配建议跑一次诊断，把输出贴给开发者即可完成选择器校准：

```bash
python3 zcode_resume_watcher.py --test-navigate <sessionId>
```

暂不支持草稿检测（发送前请留意输入框是否有未发送内容）。

## 常用命令

```bash
python zcode_resume_watcher.py                      # 前台运行(同安装版引擎)
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
  唯二例外（均可控）：① 启用 B2 LLM 判官时，会向 `judge_endpoint` 发送**截断 400 字符的
  待判文本片段**（默认指向本机 `127.0.0.1`，不出机器；若你把它配成远程地址，这些片段才会离机）；
  ② Windows 引导脚本从 **python.org 官方源**下载便携版 Python（HTTPS 直连，无镜像、
  无哈希锁定——介意者可自装 Python 后再运行）。

## 已知限制

- ZCode 升级可能改动日志字段或 UI 结构（UIA 类名等）——`--audit` 的签名统计是早期预警，多数适配改 `config.json` 即可；
- ZCode 会定期清理 rollout 目录，历史 model-io 文件会消失（监听已兼容）；
- macOS v1 智能归位选择器待真机校准，草稿检测缺失；`.app` 菜单栏入口已提供构建脚本，尚未真机验收；
  源码运行无菜单栏（回退信号循环）；
- 桌面面板依赖系统 Edge；Edge app 窗标题栏/任务栏图标已设为本应用图标，但 Edge 浏览器进程本身仍会常驻；
- 监视器进程本身可能静默消失（无自动拉起，作者的有意取舍）——建议偶尔跑一次 `--audit` 兜底。

## 更新

- 启动时若配置 `check_updates: true`（默认开启），日志会提示是否有新版本；
- `git pull` 或重新下载 [Releases](../../releases) 里的 zip 覆盖即可；配置与日志文件不会被覆盖。

## 相关链接

- [architecture.html](architecture.html) — 交互式架构页（信号管线 / 11 场景 / 守卫语义）
- [tests/driver.py](tests/driver.py) — 112 项引擎合成回归（`python tests/driver.py`）
- [tests/web_shell/test_web_shell.py](tests/web_shell/test_web_shell.py) — 12 项桌面壳接缝测试（`python -m unittest discover -s tests/web_shell`）
- CI 在 Windows/macOS/Linux 三平台运行以上两套测试

## License

[Apache-2.0](LICENSE)

> 本项目为社区工具，与 Z.ai / 智谱官方无关；"ZCode" 是其各自所有者的商标。

---

## English (TL;DR)

**zcode-resume-watcher** keeps ZCode desktop agent sessions alive. When a turn dies from network failures, empty responses, rate limits, or provider "fake success" errors (error text returned as a normal answer — invisible to ZCode's own error channel), the watcher detects it from local logs and automatically sends "the task was interrupted, please continue!" back to the affected session.

- **Detection**: dual-channel (event log + model-IO log), unknown error types covered by default, user-initiated cancellations and hopeless errors never triggered, per-session circuit breaker (5 sends, reset on success).
- **Smart re-focus** (Windows): resolves the session title from the local database, clicks the sidebar entry, verifies via the header title, and aborts if a draft is present (better to miss one than to send into the wrong conversation).
- **Fully local**: zero third-party dependencies, no telemetry; `--check-updates` only pings the GitHub API (disable via `check_updates: false`).
- **Desktop panel** (v1.2.0): a local single-file HTML panel in an Edge/Chrome app window (Windows/macOS) + tray/menu bar; 24h recovery list, four-stage chain, light/dark themes, pause/test-send, remembered window size. Closing the window keeps the engine running; use the tray/menu bar to quit.
- **Platforms**: Windows full-featured; macOS experimental (AppleScript injection + AX-based smart re-focus in beta; Accessibility permission required; no draft-check yet).
- **Install**: v1.2.0 is not released yet. Download `ZCodeResumeWatcher-<version>-Setup.exe`
  (and the macOS DMG) from the **Desktop Build** workflow run artifacts, or build from source
  (`packaging/README.md`). Once v1.2.0 is released, the installer will be attached to Releases.
  The installer is per-user by default with selectable directory/all-users, Start Menu + desktop
  shortcut, optional login start, bundled Python. Or run from source with
  `python ZCode续命面板.pyw` (Python 3.12+). macOS: build the menu-bar `.app` with
  `packaging/build_macos.sh`, or run `python3 ZCode续命面板.pyw` from source.
  See the Chinese sections above for full details.
