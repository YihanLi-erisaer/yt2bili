# yt2bili

把**你有权转载**的单条 YouTube 视频下载下来，用 DeepL 把标题和简介译成中文，再投稿到 B 站。

只支持一条链接。创作声明为「内容无需标注」，简介末尾会带上原标题、原作者和原链接。

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

失败后续跑（不会无故重下已有的 `video.mp4`）：

```powershell
python -m yt2bili retry VIDEO_ID
```

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
| 分辨率 | yt-dlp 能下到的最高画质；已是 MP4 则不转码，其它格式再转成 MP4 | 无上限 |
| 时长 | 不限制 | — |

年龄限制等需要登录才能看的 YouTube 视频：从浏览器导出 Netscape 格式 cookies，放到例如 `secrets\youtube_cookies.txt`，并在 `.env` 中设置：

```
YOUTUBE_COOKIES=secrets/youtube_cookies.txt
```

## 流程

1. 解析链接元数据  
2. 下载最高画质；已是 MP4 则跳过转码，否则转成 MP4  
3. 下载封面，裁成 1280×720 JPEG；失败则从视频抽帧  
4. DeepL Free 翻译标题和简介（已是中文则跳过），标题截到 80 字  
5. 调用 `biliup upload` 提交稿件（创作声明：内容无需标注）  

`--dry-run` 在第 5 步之前停下。Cookie 过期时重新 `login`。投稿成功只表示已进入审核，不表示已过审。投稿成功后会自动删除该稿件在 `work\` 下的本地文件（视频、封面等），任务记录仍留在数据库里。

非正式会员大约每天最多 5 条；上传过快会被限流，稍等再 `retry`。
