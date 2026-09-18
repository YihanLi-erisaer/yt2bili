# yt2bili

把**你有权转载**的 YouTube 视频下载下来，用 DeepL 把标题和简介译成中文，再投稿到 B 站。

支持一次传入多条链接：下载和封面处理可以并行，B 站上传会自动排队（同一账号不并行投稿）。创作声明为「内容无需标注」，简介末尾会带上原标题、原作者和原链接。

## 使用前

请确认视频属于下列情形之一：你拥有版权、已获授权、或源平台明确允许转载。YouTube 用户协议和 B 站社区规范都可能禁止未授权搬运。

## 环境

- Windows 10/11（也可用手动安装的 biliup 在其它系统上跑）
- Python 3.11+
- [FFmpeg](https://ffmpeg.org/download.html)（`ffmpeg` 和 `ffprobe` 都要在 PATH 里）
- [DeepL API Free](https://www.deepl.com/pro-api) 密钥（以 `:fx` 结尾）
- 可以正常网页投稿的 B 站账号

## 安装

在项目目录：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

用编辑器打开 `.env`，填入 `DEEPL_AUTH_KEY`。密钥不要发给任何人，也不要提交到 Git。

下载投稿工具并检查 FFmpeg：

```powershell
python -m yt2bili setup
```

这会把官方 [biliupR](https://github.com/biliup/biliup/releases/latest)（原 biliup-rs）放到 `bin\biliup.exe`。也可以自己下载 `biliupR-*-x86_64-windows.zip`，把 `biliup.exe` 放进 `bin\`。

B 站扫码登录（Cookie 写到 `secrets\bili_cookies.json`，已在 `.gitignore` 中）：

```powershell
python -m yt2bili login
```

用手机 B 站 App 扫码并确认。

## 使用

先试跑（下载、封面、翻译，不投稿）：

```powershell
python -m yt2bili run "https://www.youtube.com/watch?v=xxxxxxxxxxx" --dry-run
```

确认标题和简介没问题后再投稿：

```powershell
python -m yt2bili run "https://www.youtube.com/watch?v=xxxxxxxxxxx"
```

一次处理多条（并行下载，上传排队）：

```powershell
python -m yt2bili run "https://www.youtube.com/watch?v=aaa" "https://www.youtube.com/watch?v=bbb" -j 3
```

或从文本文件读取链接（每行一条，`#` 开头为注释）：

```powershell
python -m yt2bili run --file urls.txt
```

失败后续跑（不会无故重下已有的 `video.mp4`）：

```powershell
python -m yt2bili retry VIDEO_ID
```

video_id 以 `-` 开头时也可以直接写，例如 `python -m yt2bili retry -GiIT0fNvW8`。

其它命令：

```powershell
python -m yt2bili list
python -m yt2bili renew
```

产物在 `work\<video_id>\`，任务记录在 `data\tasks.sqlite`。

## 默认投稿参数

| 项 | 默认 | 如何改 |
|----|------|--------|
| 分区 | `171`（知识 · 野生技术协会，biliup 默认） | `.env` 里 `BILI_TID` |
| 上传线路 | `tx`（避开 Windows 上证书常失效的 bldsa） | `.env` 里 `BILI_LINE`，如 `bda2` / `qn` |
| 标签 | `转载`（B 站投稿需要至少一个标签） | `.env` 里 `BILI_TAGS` |
| 创作声明 | 内容无需标注（Web 投稿，不勾选自制禁转） | 固定 |
| 分辨率 | 源站最高可用画质（含 4K）。禁止嵌入的视频可能只能下到 1080p | 无上限 |
| 时长 | 不限制 | — |
| 并行下载 | Windows 默认 1 路，其它系统 2 路（最大 8） | `-j` 或 `.env` 里 `DOWNLOAD_JOBS` |
| 上传间隔 | 30 秒 | `.env` 里 `UPLOAD_GAP_SECONDS` |

YouTube 现在要求 JS 运行时才能完整解析。本机有 Node.js 或 Deno 即可（`setup` 会检测）。若出现「Sign in to confirm you’re not a bot」，先完全退出 Edge/Chrome，再导出 cookies：

```powershell
python -m yt2bili youtube-cookies
```

之后会自动使用 `secrets\youtube_cookies.txt`。也可以在 `.env` 里写 `YOUTUBE_COOKIES_FROM_BROWSER=edge`（每次下载前都要退出浏览器）。

## 流程

1. 解析链接元数据  
2. 下载最高画质并完整解码检查音视频轨道；完整 MP4 直接上传原文件（包括 AV1/VP9，不重新编码、不降低分辨率）。只有非 MP4 文件才转换为 H.264/yuv420p + AAC 的 MP4
3. 下载封面，裁成 1280×720 JPEG；失败则从视频抽帧  
4. DeepL Free 翻译标题和简介（已是中文则跳过），标题截到 80 字  
5. 调用 `biliup upload` 提交稿件（创作声明：内容无需标注）  

`--dry-run` 在第 5 步之前停下并保留文件。Cookie 过期时重新 `login`。上传成功并获得 BV 号后，自动删除当前任务的 `work\<video_id>\` 目录（含源视频、封面、临时文件及隔离的旧文件），保留数据库中的任务记录和 BV 号。上传失败或未解析到 BV 号时保留文件；清理失败也不会把已提交的任务改成上传失败。投稿成功不代表平台转码或审核成功，删除后若平台处理失败，需要重新下载。

平台转码失败时，可生成完整、兼容的替换文件，命令不会重复投稿或修改原 BV 号：

```powershell
python -m yt2bili repair --redownload bHEuq3isf9M hQ0JDjDFJE0
```

旧文件保留在各任务的 `rejected/` 下；修复并完整校验成功后，使用日志显示的文件路径在 B 站创作中心替换原稿件的视频（直接复用 MP4 时通常为 `work\<video_id>\source.mp4`）。`repair` 不上传、不触发上传后清理。省略 `--redownload` 会先校验并尝试复用已有完整文件。完整解码校验仍会运行 FFmpeg，但不会生成转码视频。

只有非 MP4 需要转换时，`repair --encoder h264_nvenc` 才会使用 NVIDIA 编码加速，默认使用 CPU 的 `libx264`。这些选项不会强制转换已有 MP4。修复中断后去掉 `--redownload` 重新执行以续传。

可加 `--max-size-gb 8` 检查每个文件不超过 8 GB（十进制，非对平台限制的声明）。MP4 超限会报错，不自动压缩或转码；非 MP4 转换时按时长限制码率并保留分辨率。默认不限制大小。

完整校验默认优先尝试 NVIDIA GPU 解码（当前支持普通 8-bit 4:2:0 的 AV1/H.264/VP9）；没有可用设备、驱动/解码器不支持或 GPU 校验出错时，自动从头用 CPU 复核。其它格式使用 CPU，音频也使用 CPU。无需手动设置环境变量。可设置 `$env:YT2BILI_HWACCEL='cpu'` 强制 CPU，或设为 `'auto'` 恢复自动选择。CPU 处理 AV1 时建议使用包含 `libdav1d` 的 FFmpeg full 构建。

完整校验成功后，会在视频旁写入 `<文件名>.validation.json` 缓存，程序重启后仍可复用。每次先检查轨道信息并计算**整文件 SHA-256**，只有内容、文件状态、预期时长、解码模式、校验规则版本及 FFmpeg/ffprobe 工具状态均匹配才跳过重复解码；仍需顺序读取整个文件，但无需再次逐帧解码。文件中间内容变化（即使大小和修改时间不变）也会使缓存失效。失败或中断的校验不写成功缓存；缓存损坏时重新校验，缓存写入失败不影响已通过的结果。上传后清理任务目录时缓存一并删除。

需要强制重新完整校验时，设置 `$env:YT2BILI_VALIDATION_CACHE='0'`；恢复默认缓存用 `Remove-Item Env:YT2BILI_VALIDATION_CACHE`。以上设置也可写入 `.env`。这只影响校验方式，完整 MP4 仍直接上传，不会重新转码。

`.part` 文件只由 yt-dlp 续传、完成后改名，程序不再根据文件头的时长把它提前当作成品。分片下载失败、解码报错、音视频长度不匹配会阻止上传。FFmpeg 可安装到 PATH，也可将 `ffmpeg.exe` 和 `ffprobe.exe` 放在项目 `bin/` 中。

批量传入多条链接（或使用 `--file`）时，采用有界异步流水线：上一条下载、合并结束后就释放下载名额，**上一条完整校验时，下一条可以开始下载**，不必等待校验或上传结束。任务由线程池调度，逐帧校验由独立 FFmpeg 子进程执行。Windows 同时只进行 1 路下载/合并（防文件占用错误），但会启用 2 个流水线任务，即使指定 `-j 1` 也能重叠下载和校验；其它系统下载并发由 `-j` 控制，另保留 1 个流水线任务名额。预下载数量有限，不会无限堆积大文件。

补音频和失败重试同样遵守下载名额限制；校验、缓存指纹计算和重试等待不占下载名额。校验失败的文件仍会隔离重下，不会提前上传。GPU/CPU 校验进度约每 5 秒输出一次，包含视频 ID、轨道、已检查时长和百分比；命中缓存时只提示跳过重复解码。DeepL 翻译和 B 站上传仍然串行，避免限流。其中一条失败不会中断其它条。已经投稿成功的视频在批量模式下会跳过（单条仍会报错，可用 `--force` 重做）。

非正式会员大约每天最多 5 条；上传过快会被限流，稍等再 `retry`。不要对同一账号并行打开多个上传进程。
