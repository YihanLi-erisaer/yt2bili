# Windows 应用与安装包

适用于 Windows 10/11 x64。版本为 `0.2.0-alpha.1`。

## 安装和运行

- **安装版**：运行 `yt2bili_0.2.0-alpha.1_x64-setup.exe`，按向导安装，随后从开始菜单的 StarDazz 文件夹启动。
- **免安装版**：完整解压 `yt2bili_0.2.0-alpha.1_x64-portable.zip`，双击 `yt2bili/yt2bili.exe`。也可以直接运行构建输出 `dist/windows/yt2bili/yt2bili.exe`。必须保留相邻的 `worker` 和 `bin` 文件夹，不能只复制主 EXE。
- 已包含 Python 后台、FFmpeg、ffprobe、biliup 和 Node.js，无需安装开发工具。界面仍需要 Microsoft WebView2 Runtime；安装版会在缺失时联网安装，免安装版需要电脑已有此运行时。
- 本地翻译模型首次使用时在应用内安装，模型不包含在安装包里。DeepL 为可选项。抖音官方同步仍需要单独配置授权服务。
- 本次安装包未做代码签名。

## 用户数据

安装版与免安装版默认共用 `%LOCALAPPDATA%/StarDazz/yt2bili/`，其中保存设置、任务和凭据；默认素材也存放在该目录。免安装指程序无需安装，数据不会跟随应用目录移动。不要同时运行开发版和发布版处理同一份数据。

可通过 Windows“已安装的应用”卸载安装版。应用程序和用户数据分开保存；卸载后如果仍需要任务记录和素材，请保留上述数据目录。没有配置自动更新，升级时先退出应用，再运行新的安装包。

## 重复构建

需要 Windows x64、Node.js 22、Rust/MSVC、Visual Studio C++ 构建工具，以及安装了 `requirements-desktop-lock.txt` 的 Python 3.12 环境。仓库 `bin/` 中须有 `ffmpeg.exe`、`ffprobe.exe`、`biliup.exe`，前端先在 `desktop` 下运行 `npm ci`。

在仓库根目录：

```powershell
$env:YT2BILI_BUILD_PYTHON = 'C:\path\to\python.exe'
node desktop/scripts/package-windows.mjs
```

也可双击根目录 `打包Windows.cmd`。未设置解释器时优先使用 `.desktop-dev/package-python`，其次 `.desktop-venv`。构建会冻结后台、构建前端与 Tauri release、生成 NSIS 安装包、检查随包工具和原生通信，再压缩免安装版并计算 SHA-256。输出在 `dist/windows/`。

发布配置：`desktop/src-tauri/tauri.windows-release.conf.json`。安装包使用当前用户安装模式，支持简体中文、繁体中文、英文。构建首次运行可能需要联网获取 NSIS 等组件。Tauri 安装器说明：https://v2.tauri.app/distribute/windows-installer/

自动化检查覆盖本机启动与通信，不代表干净 Windows 虚拟机兼容性验证，也不代表真实账号上传或真实模型质量验收。
