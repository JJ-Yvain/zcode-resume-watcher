# 桌面入口打包

目标：用户只看到 **一个 APP 入口**，不再面对 `启动续命面板.bat` / `.command` 等脚本。

- **Windows**：`ZCodeResumeWatcher.exe`（PyInstaller onedir）+ Inno Setup 安装包；
  安装后开始菜单/桌面快捷方式指向 exe，可选开机启动。
- **macOS**：`ZCode 续命监视器.app`（PyInstaller `.app`，`LSUIElement` 菜单栏应用）+ DMG；
  用户从菜单栏打开面板，不再需要 Terminal。
- 面板仍是本地 HTML + 浏览器/Edge `--app` 窗口；引擎和判定逻辑不变。

## Windows 构建

前置：Python 3.12、Inno Setup 6。

```powershell
python -m pip install -r packaging/requirements-build.txt
winget install --id JRSoftware.InnoSetup -e
python packaging/build_windows.py
```

输出：

```text
dist/ZCodeResumeWatcher/ZCodeResumeWatcher.exe
dist/installer/ZCodeResumeWatcher-<version>-Setup.exe
```

安装包默认**当前用户安装**（`{autopf}\ZCodeResumeWatcher`，普通权限即可），
安装向导会显示目录选择页，可改到任意盘符；安装模式对话框也可选“为所有用户安装”（需要管理员）。
桌面快捷方式默认勾选（可取消），登录启动默认不勾选。
运行态数据始终写用户目录，不跟随安装盘符。
`dist/ZCodeResumeWatcher/ZCodeResumeWatcher.exe` 本身就是便携版，双击即可运行。

## macOS 构建

必须在 macOS 上构建（PyInstaller 不支持交叉编译）。

```bash
python3 -m pip install -r packaging/requirements-build.txt
bash packaging/build_macos.sh
```

输出：

```text
dist/ZCode 续命监视器.app
dist/ZCodeResumeWatcher-<version>.dmg
```

`build_macos.sh` 默认 ad-hoc 签名；正式分发时设置 `ZRW_SIGN_IDENTITY`，
并另外做 notarization。

## 运行态数据

安装版不再写应用目录（Program Files / 只读 .app）：

- Windows：`%LOCALAPPDATA%\ZCodeResumeWatcher`
- macOS：`~/Library/Application Support/ZCodeResumeWatcher`

包含 `config.json`、`watcher.log`、`navigate.log`、`ui_state.json`、
`panel_runtime.json`、`zcode-log/`、`rollout/`、`db.sqlite` 等。
首次启动会把包内 `config.json` 模板复制过去。

## CI

`packaging/desktop-build.yml` 是双平台构建模板（workflow_dispatch + v* tag）：

- Windows：PyInstaller onedir + Inno Setup 安装包
- macOS：PyInstaller `.app` + ad-hoc 签名 + DMG

公共仓使用时复制到 `.github/workflows/desktop.yml`。macOS 产物由 CI 生成，
不需要本地 Mac；正式发布前仍需在真机做一次菜单栏/面板验收。

## 说明

- 引擎仍是纯标准库；打包依赖只在构建期（PyInstaller）和 macOS 菜单栏
  （rumps/PyObjC）出现，终端用户无需安装 Python。
- Windows 构建使用 onedir（启动快、无 onefile 解压）；安装包负责收拢成开始菜单入口。
- macOS `.app` 使用 `LSUIElement=True`，无 Dock 图标，菜单栏常驻。
