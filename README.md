# yt2bili

把**你有权转载**的 YouTube 视频下载下来，默认用本地开源大模型把标题和简介译成中文（也可优先使用 DeepL），再投稿到 B 站。

支持一次传入多条链接：下载和封面处理可以并行，B 站上传会自动排队（同一账号不并行投稿）。创作声明为「内容无需标注」，简介末尾会带上原标题、原作者和原链接。

## 可视化桌面版（第一期）

现已提供与 StarDazz 官网风格一致的桌面开发版本，支持任务管理、素材预览与编辑、扫码登录、设置、日志和投稿操作。

当前电脑可双击根目录 `启动桌面版.cmd`，或执行 `cd desktop` 后运行 `npm run desktop`。首次使用请从任务中心的“开始配置”进入。

开发环境与使用方法见 [桌面版说明](desktop/README.md)，实现与测试记录见 [第一期开发与验收记录](第一期开发与验收记录.md)。Windows 安装 EXE 和 macOS DMG 在后续两期交付。以下仍是原有 CLI 用法。

## 使用前

请确认视频属于下列情形之一：你拥有版权、已获授权、或源平台明确允许转载。YouTube 用户协议和 B 站社区规范都可能禁止未授权搬运。

## 环境

- Windows 10/11（也可用手动安装的 biliup 在其它系统上跑）
- Python 3.11+
- [FFmpeg](https://ffmpeg.org/download.html)（`ffmpeg` 和 `ffprobe` 都要在 PATH 里）
- 本地翻译组件，或可选的 [DeepL API](https://www.deepl.com/pro-api) 密钥
- 可以正常网页投稿的 B 站账号

## 安装

在项目目录：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

默认本地优先，执行 `python -m yt2bili translation setup` 安装固定版本的 Ollama 和 Qwen3 8B，再用 `python -m yt2bili translation test` 试译。首次下载约 6.7 GB；安装会检查空间。

如需 DeepL，在 `.env` 填入可选的 `DEEPL_AUTH_KEY`，设置 `TRANSLATION_PRIMARY=deepl` 可优先使用它。`TRANSLATION_FALLBACK_ENABLED=false` 关闭自动切换。密钥不要提交到 Git。详细部署、离线导入与验收边界见[本地翻译实现说明](docs/本地翻译实现与验证.md)。

下载投稿工具并检查 FFmpeg：

```powershell
python -m yt2bili setup
```

这会把官方 [biliupR](https://github.com/biliup/biliup/releases/latest)（原 biliup-rs）放到 `bin\biliup.exe`。也可以自己下载 `biliupR-*-x86_64-windows.zip`，把 `biliup.exe` 放进 `bin\`。

添加 Bilibili 账号（最多 5 个不同 UID，凭据保存在忽略提交的 secrets 目录）：

```powershell
python -m yt2bili accounts add
python -m yt2bili accounts list
```

## 使用

每次只输入一个 YouTube 视频链接，必须用 `--account` 选择账号。默认只准备素材，完成后在桌面预览编辑并确认投稿；也可用 CLI 确认：

```powershell
python -m yt2bili run "https://www.youtube.com/watch?v=xxxxxxxxxxx" --account ACCOUNT_ID
python -m yt2bili submit TASK_ID
```

明确需要自动投稿时增加 `--auto`。不再支持多 URL、TXT 列表、`--file` 或 `--force`。同视频同账号返回原任务，同视频可分别创建到不同账号。

```powershell
python -m yt2bili run "https://youtu.be/xxxxxxxxxxx" --account ACCOUNT_ID --auto
python -m yt2bili retry TASK_ID
python -m yt2bili repair TASK_ID
python -m yt2bili login --account ACCOUNT_ID
python -m yt2bili renew --account ACCOUNT_ID
python -m yt2bili accounts archive --account ACCOUNT_ID
python -m yt2bili list
```

重试始终回到预览，目标账号不能改变。归档只允许该账号全部任务已提交；清除登录凭据仍占账号名额。重新添加已归档的 UID 会恢复原账号身份。

新素材目录为 `work/<task_id>/`，历史视频 ID 有且仅有一个匹配时才允许作为 CLI 参数。GUI 与 CLI 不能同时执行同一个数据目录；指定同一份库时先退出桌面：

```powershell
python -m yt2bili --data-dir "C:\\path\\to\\profile" list
```

启动会备份旧数据库并升级到 schema v3（兼容旧单账号库及两种 v2 多账号/翻译库）。旧任务不会自动判断历史投稿账号，需在桌面详情中一次性绑定。旧 Cookie 能确定本地 UID 时仅导入为待验证账号，原文件保留。新旧版本不支持混跑；回滚需先另存升级后的数据库，再恢复升级前备份及对应旧版本，不能直接降低 schema 版本号。备份后新增任务不在旧库中，素材目录应保留。

## 默认投稿参数

| 项 | 默认 | 如何改 |
|----|------|--------|
| 分区 | `171`（知识 · 野生技术协会，biliup 默认） | `.env` 里 `BILI_TID` |
| 上传线路 | `tx`（避开 Windows 上证书常失效的 bldsa） | `.env` 里 `BILI_LINE`，如 `bda2` / `qn` |
| 标签 | `转载`（B 站投稿需要至少一个标签） | `.env` 里 `BILI_TAGS` |
| 创作声明 | 内容无需标注（Web 投稿，不勾选自制禁转） | 固定 |
| 分辨率 | 源站最高可用画质（含 4K）。禁止嵌入的视频可能只能下到 1080p | 无上限 |
| 时长 | 不限制 | — |
| 队列并发 | 下载、校验各共享 1 路；每账号上传 1 路，最多 5 路 | 账号队列自动创建 |
| 上传间隔 | 同 UID 上一次上传尝试结束后至少 20 秒再开始下一次 | `.env` 里 `UPLOAD_GAP_SECONDS` |

YouTube 现在要求 JS 运行时才能完整解析。本机有 Node.js 或 Deno 即可（`setup` 会检测）。若出现「Sign in to confirm you’re not a bot」，先完全退出 Edge/Chrome，再导出 cookies：

```powershell
python -m yt2bili youtube-cookies
```

之后会自动使用 `secrets\youtube_cookies.txt`。也可以在 `.env` 里写 `YOUTUBE_COOKIES_FROM_BROWSER=edge`（每次下载前都要退出浏览器）。

## 流程

1. 解析链接元数据  
2. 下载最高画质并完整解码检查音视频轨道；完整 MP4 直接上传原文件（包括 AV1/VP9，不重新编码、不降低分辨率）。只有非 MP4 文件才转换为 H.264/yuv420p + AAC 的 MP4
3. 下载封面，裁成 1280×720 JPEG；失败则从视频抽帧  
4. 按用户选择的优先级翻译标题和简介（默认本地，失败可切换 DeepL；已是中文则跳过），标题截到 80 字
5. 调用 `biliup upload` 提交稿件（创作声明：内容无需标注）  

`--dry-run` 在第 5 步之前停下并保留文件。Cookie 过期时使用 `login --account ACCOUNT_ID`。上传成功并获得 BV 号后，自动删除当前任务的 `work\<video_id>\` 目录（含源视频、封面、临时文件及隔离的旧文件），保留数据库中的任务记录和 BV 号。上传失败或未解析到 BV 号时保留文件；清理失败也不会把已提交的任务改成上传失败。投稿成功不代表平台转码或审核成功，删除后若平台处理失败，需要重新下载。

平台转码失败时，可生成完整、兼容的替换文件，命令不会重复投稿或修改原 BV 号：

```powershell
python -m yt2bili repair TASK_ID
```

修复会先尝试复用本任务完整素材，缺失或损坏时重新准备。成功后使用日志显示的路径在创作中心替换原稿件视频；不上传、不修改 BV、不触发成功清理。

完整校验默认优先尝试 NVIDIA GPU 解码（当前支持普通 8-bit 4:2:0 的 AV1/H.264/VP9）；没有可用设备、驱动/解码器不支持或 GPU 校验出错时，自动从头用 CPU 复核。其它格式使用 CPU，音频也使用 CPU。无需手动设置环境变量。可设置 `$env:YT2BILI_HWACCEL='cpu'` 强制 CPU，或设为 `'auto'` 恢复自动选择。CPU 处理 AV1 时建议使用包含 `libdav1d` 的 FFmpeg full 构建。

完整校验成功后，会在视频旁写入 `<文件名>.validation.json` 缓存，程序重启后仍可复用。每次先检查轨道信息并计算**整文件 SHA-256**，只有内容、文件状态、预期时长、解码模式、校验规则版本及 FFmpeg/ffprobe 工具状态均匹配才跳过重复解码；仍需顺序读取整个文件，但无需再次逐帧解码。文件中间内容变化（即使大小和修改时间不变）也会使缓存失效。失败或中断的校验不写成功缓存；缓存损坏时重新校验，缓存写入失败不影响已通过的结果。上传后清理任务目录时缓存一并删除。

需要强制重新完整校验时，设置 `$env:YT2BILI_VALIDATION_CACHE='0'`；恢复默认缓存用 `Remove-Item Env:YT2BILI_VALIDATION_CACHE`。以上设置也可写入 `.env`。这只影响校验方式，完整 MP4 仍直接上传，不会重新转码。

`.part` 文件只由 yt-dlp 续传、完成后改名，程序不再根据文件头的时长把它提前当作成品。分片下载失败、解码报错、音视频长度不匹配会阻止上传。FFmpeg 可安装到 PATH，也可将 `ffmpeg.exe` 和 `ffprobe.exe` 放在项目 `bin/` 中。

任务使用两个共享队列及 0～5 个账号上传队列：

- 下载队列负责解析、下载、合并，完成后即处理下一条。
- 校验队列保留完整解码、GPU/CPU 复核及缓存逻辑。校验失败隔离旧文件并回到下载队尾，最多五轮。
- 每个账号单独准备封面、翻译和上传。等待登录、冷却或限流不会占用其他账号；等待期间该账号后续预览任务仍可准备。

账号间可同时上传，同 UID 即使使用不同 profile 或 Cookie 路径也共享操作系统锁与冷却时间。未知投稿必须先核对创作中心，不会自动重发；异常退出后其他未完成任务也需手动继续。正常退出会取消未投稿任务并等待所有在途投稿结束。

`accounts.resume_uploads`（桌面“恢复上传”）用于人工核对后的限流或异常占用恢复。残留上传进程仍存在时拒绝恢复；恢复队列不会解除任务自身的“待核对”保护。

完整设计见 [PRD](docs/多账号上传与单链接任务PRD.md) 和 [技术实现方案](docs/多账号上传与单链接任务技术实现方案.md)。真实平台五账号验收与自动化模拟验证分开记录。
