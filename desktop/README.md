# yt2bili 桌面端 · 第一期

版本：`0.2.0-alpha.1`。提供 React + Tauri 原生桌面应用，连接现有 Python 视频业务。Windows 安装包和免安装应用的使用及构建见 [Windows 安装与打包](../docs/Windows安装与打包.md)。macOS DMG 尚未提供。

## 在当前电脑启动

双击项目根目录的 **`启动桌面版.cmd`**，或者在项目根目录运行：

```powershell
cd desktop
npm run desktop
```

本次已在本机准备 `.desktop-venv`、前端依赖和项目内 `.toolchains` Rust 工具链；它们不提交到 Git。启动脚本会优先使用这些工具。首次编译可能需要等待，Tauri 窗口出现后即可操作。

开发服务使用 `127.0.0.1:1420`，该端口必须空闲。它只服务前端文件；后台业务通过进程管道通信。直接在浏览器打开开发地址不会连接真实后台。

Windows 启动器会合并 `PATH` / `Path`，保留 Node/npm、系统工具和项目 Rust 工具链的路径。启动前也会检查 Python 依赖，依赖不匹配时直接输出修复说明。

如果曾使用另一版本 Python 覆盖创建 `.desktop-venv`，旧版二进制包可能仍留在其中，出现 `PIL._imaging` 导入失败。仅再次运行普通 `pip install` 可能显示已满足依赖；请在项目根目录按当前解释器重新安装：

```powershell
.desktop-venv/Scripts/python.exe -m pip install --force-reinstall -r requirements-desktop-lock.txt
.desktop-venv/Scripts/python.exe -m pip check
```

## 新电脑开发环境

Windows x64 需要 Python 3.12、Node.js 22、Rust stable、Visual Studio C++ 桌面开发工具和 WebView2 Runtime。

在仓库根目录执行：

```powershell
python -m venv .desktop-venv
.desktop-venv/Scripts/python.exe -m pip install -r requirements-desktop-lock.txt
cd desktop
npm ci
npm run desktop
```

`requirements-desktop-lock.txt` 固定本次验证过的 **Windows / Python 3.12** 依赖，其中包含 Windows 专属包；其他平台开发时先使用 `requirements-desktop.txt`，第三期再生成按平台验证的锁文件。前端与 Rust 分别使用 `package-lock.json`、`Cargo.lock`。

仓库 `bin/` 下准备 `ffmpeg.exe`、`ffprobe.exe`、`biliup.exe`，也可使用 PATH 中的工具。本次本机适配的 biliup 为 `1.2.4`。YouTube 下载还需要可用的 Node.js 或 Deno。开发版不自动下载或偷偷替换这些二进制，环境页会显示实际工具路径及版本。Windows 发布构建会随包分发这些工具。

## 首次使用

1. 在任务中心点击“开始配置”，选择素材目录并完成环境检查。
2. 默认安装并试译本地大模型；也可选择 DeepL 优先并保存密钥。DeepL 密钥选填，自动切换可关闭。
3. 使用 B 站 App 扫码；也可暂时跳过，先准备素材。已有 biliup 登录 JSON 可在“账号与连接”导入。
4. 必要时导入 Netscape 格式的 YouTube Cookie TXT，或从 Edge、Chrome、Firefox 导出。浏览器锁定、加密或权限问题会返回错误。
5. 点击“添加任务”，粘贴单条/多条链接或导入 TXT。默认“先预览，再投稿”。
6. 素材准备好后打开详情，查看封面、原文和译文，保存修改，再明确确认投稿。

每批最多 200 条，按视频 ID 去重。下载、校验、上传各一个工作线程，跨阶段并行。点击重试始终先回到预览，不会直接重投。上传中不能取消；关闭窗口时可先取消未提交任务并等待上传结束。

上传成功仅代表已提交，不代表审核通过。没有 BV 号或提交过程断开时进入“投稿待核对”，请到创作中心核对并登记 BV 号，或明确确认尚未提交后再继续。修复已投稿任务只准备本地替换素材，不会再次投稿。

## 数据、凭据与旧项目

Windows 默认用户数据目录是 `%LOCALAPPDATA%/StarDazz/yt2bili/`：

- `settings.json`：非敏感设置。
- `data/tasks.sqlite`：任务、配置快照及操作去重记录。
- `work/`：默认素材目录，可在设置中调整。
- `logs/desktop.log`：脱敏后的轮转日志，每份最多约 2 MB，保留三份历史；日志导出包含当前和历史记录。
- `secrets/`：biliup 兼容登录文件与 YouTube Cookie。使用当前用户目录权限；不将原始 Cookie 返回界面。

本地翻译配置在“设置”或“账号与连接”中管理，安装后可断网试译。组件保存在用户目录的 `translation/`，不会随视频素材清理。实现及限制见[本地翻译说明](../docs/本地翻译实现与验证.md)。

DeepL 密钥保存到系统凭据存储，不写入普通设置或任务快照。桌面版不自动读取项目 `.env`。CLI 保持原有配置方式。

设置页可选择旧项目根目录导入 `data/tasks.sqlite`。导入前请停止旧任务；应用使用 SQLite 备份并核对素材路径，保留旧库和素材位置，不复制 `.env` 或账号密钥。路径不符合旧项目 `work/<video_id>` 结构的素材引用会被清空，需要重新准备。

桌面端单实例；GUI 和更新后的 CLI 对相同素材目录使用同一文件锁，并通过当前系统用户的上传锁避免同时提交。不同数据目录的独立历史不会自动合并，使用两个入口前仍应先核对历史。

## 实现结构

```text
desktop/src/                 React 页面、品牌主题、配置向导、Tauri 通信桥
desktop/src-tauri/           原生窗口、管道通信、单实例、子进程生命周期
yt2bili/desktop_worker.py    JSON Lines 协议入口与脱敏日志
yt2bili/desktop_service.py   受限方法、任务/账号/配置/导入服务
yt2bili/scheduler.py         三个长期运行的 FIFO 阶段队列
yt2bili/desktop_auth.py      biliup BiliTV 扫码协议适配
yt2bili/desktop_settings.py  非敏感配置与系统密钥存储
yt2bili/events.py            任务进度与取消令牌
yt2bili/locking.py           进程锁与跨进程投稿间隔
yt2bili/pipeline.py          GUI / CLI 共用的视频处理和投稿核心
scripts/                    后台冻结、源代码/冻结后台/原生窗口烟雾测试
```

扫码适配依据 biliup 上游的 [credential.rs](https://github.com/biliup/biliup/blob/master/crates/biliup/src/uploader/credential.rs)，直接生成兼容的 LoginInfo JSON，避免依赖终端菜单。登录成功、过期、取消与旧会话覆盖使用模拟响应测试；真实扫码仍需要用户手机验收。

通信协议与验证结果见 [第一期开发与验收记录](../第一期开发与验收记录.md)。

## 开发与验证命令

在仓库根目录：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$env:YT2BILI_BIN_DIR = Join-Path (Get-Location) 'bin'
.desktop-venv/Scripts/python.exe -m unittest discover -s tests -v
.desktop-venv/Scripts/python.exe scripts/smoke_worker.py
.desktop-venv/Scripts/python.exe scripts/build_worker.py
.desktop-venv/Scripts/python.exe scripts/smoke_worker.py --frozen packaging/staging/yt2bili-worker/yt2bili-worker.exe
```

前端构建、交互测试：

```powershell
cd desktop
npm run build
npm run test:e2e
npm run test:launcher
```

测试默认使用本机 Microsoft Edge。`npm run test:e2e` 会自动启动前端开发服务（或复用已有服务）。开发预览 `/?preview&populated` 明确标记示例数据，不调用外部账号，生产构建不会包含预览模块。

原生通信检查：先启动 `npm run dev`，用已安装的 Rust 工具链运行 `cargo build --manifest-path desktop/src-tauri/Cargo.toml`，然后在仓库根目录运行 `scripts/smoke_native.py`。脚本使用独立临时数据目录，在隐藏窗口验证 React → Tauri → Python → React 后自动退出。发布版检查使用 `scripts/smoke_native.py --release dist/windows/yt2bili/yt2bili.exe`，不需要 Vite；仅在显式传入 `--smoke-test` 且指定独立数据和报告环境变量时启用测试钩子。

GitHub Actions 工作流 `desktop-ci.yml` 提供 Windows 后台、前端、Rust 和冻结后台验证；本次未触发远程 CI。

## 发布验证边界

Windows 发布构建已将冻结后台、FFmpeg、biliup 和 Node.js 纳入安装资源，提供 NSIS 安装和完整目录免安装版本。仍需代码签名、干净 Windows 机器验证及自动更新。macOS 仍需真机、按架构构建、Keychain/进程退出适配、签名、公证与 DMG 验证。
