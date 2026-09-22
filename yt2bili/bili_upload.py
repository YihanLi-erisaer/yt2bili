from __future__ import annotations

import logging
import platform
import shutil
import sys
import tarfile
import zipfile
import io
import json
import re
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

from yt2bili.config import Settings
from yt2bili.exceptions import Yt2BiliError
from yt2bili import process_manager

logger = logging.getLogger(__name__)

_GITHUB_LATEST = "https://api.github.com/repos/biliup/biliup/releases/latest"
_BV_RE = re.compile(r"\b(BV[0-9A-Za-z]+)\b")
_CERT_FAIL = (
    "invalid peer certificate",
    "certificate: expired",
    "tls certificate",
)
_FALLBACK_LINES = ("tx", "bda2", "qn", "ws", "txa")


def find_biliup(settings: Settings) -> Path:
    if settings.biliup_bin:
        path = settings.biliup_bin
        if not path.is_absolute():
            path = settings.root / path
        if path.is_file():
            return path
        raise Yt2BiliError(f"BILIUP_BIN 指向的文件不存在：{path}")

    for candidate in (
        settings.bin_dir / "biliup.exe",
        settings.bin_dir / "biliup",
    ):
        if candidate.is_file():
            return candidate

    for name in ("biliup.exe", "biliup"):
        found = shutil.which(name)
        if found:
            return Path(found)

    raise Yt2BiliError(
        "未找到 biliup 命令行工具。\n"
        "请运行：python -m yt2bili setup\n"
        "或从 https://github.com/biliup/biliup/releases/latest 下载 "
        "biliupR-*-x86_64-windows.zip，解压后把 biliup.exe 放到 bin/ 目录。"
    )


def setup_biliup(settings: Settings) -> Path:
    settings.bin_dir.mkdir(parents=True, exist_ok=True)
    try:
        return find_biliup(settings)
    except Yt2BiliError:
        pass

    logger.info("正在从 GitHub 下载最新 biliupR ...")
    asset_name, asset_url = _select_asset(_latest_release())
    logger.info("下载 %s", asset_name)
    data = _http_bytes(asset_url)
    exe_name = "biliup.exe" if sys.platform == "win32" else "biliup"
    dest = settings.bin_dir / exe_name
    _extract_biliup(data, asset_name, dest)
    dest.chmod(dest.stat().st_mode | 0o111)
    logger.info("已安装到 %s", dest)
    return dest


def login(settings: Settings) -> None:
    biliup = find_biliup(settings)
    settings.bili_cookies.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(biliup), "-u", str(settings.bili_cookies), "login"]
    logger.info("启动 B 站扫码登录：%s", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise Yt2BiliError("B 站登录失败。请确认已用 App 扫码并点击确认。")
    if not settings.bili_cookies.is_file():
        raise Yt2BiliError(f"登录完成但未找到 Cookie 文件：{settings.bili_cookies}")


def renew(settings: Settings) -> None:
    biliup = find_biliup(settings)
    _require_cookies(settings)
    cmd = [str(biliup), "-u", str(settings.bili_cookies), "renew"]
    result = process_manager.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        logger.warning(
            "刷新 Cookie 失败，将继续使用现有登录态：%s",
            (result.stderr or result.stdout or "").strip(),
        )
    else:
        logger.info("已刷新 B 站登录态。")


def upload(
    settings: Settings,
    video: Path,
    cover: Path,
    title: str,
    description: str,
    source_url: str,
) -> str:
    biliup = find_biliup(settings)
    _require_cookies(settings)
    if not video.is_file():
        raise Yt2BiliError(f"视频不存在：{video}")
    if not cover.is_file():
        raise Yt2BiliError(f"封面不存在：{cover}")

    last_output = ""
    for line_name in _line_order(settings.bili_line):
        cmd = [
            str(biliup),
            "-u",
            str(settings.bili_cookies),
            "upload",
            str(video),
            "--submit",
            "web",
            "--line",
            line_name,
            "--copyright",
            "1",
            "--no-reprint",
            "0",
            "--extra-fields",
            json.dumps({"web_os": 3, "recreate": -1}, ensure_ascii=False),
            "--tid",
            str(settings.bili_tid),
            "--cover",
            str(cover),
            # Bind free-form values to their options so leading '-' characters
            # are not interpreted as biliup flags (e.g. description separators).
            f"--title={title}",
            f"--desc={description}",
            f"--tag={settings.bili_tags}",
        ]
        logger.info(
            "开始上传到 B 站（分区 tid=%s，线路 %s，Web投稿，创作声明：内容无需标注）",
            settings.bili_tid,
            line_name,
        )
        code, output = _run_logged(cmd)
        last_output = output
        if code == 0:
            bv = _parse_bv(output)
            if bv:
                logger.info("投稿已提交：%s", bv)
                return bv
            logger.info("投稿命令成功，但输出里没有解析到 BV 号。")
            return ""
        if _is_cert_failure(output):
            logger.warning("线路 %s TLS 校验失败，尝试下一条上传线路。", line_name)
            continue
        raise Yt2BiliError(
            "B 站投稿失败。若提示未登录请运行 python -m yt2bili login；"
            "若提示上传过快请等待数分钟后 retry。\n"
            f"{output.strip()[-2000:]}"
        )
    raise Yt2BiliError(
        "B 站投稿失败：多条上传线路的 TLS 证书校验都未通过。"
        "可在 .env 设置 BILI_LINE=bda2 后再试。\n"
        f"{last_output.strip()[-2000:]}"
    )


def _line_order(preferred: str) -> list[str]:
    preferred = preferred.strip().lower()
    seen: list[str] = []
    for name in (preferred, *_FALLBACK_LINES):
        if name and name not in seen and name != "bldsa":
            seen.append(name)
    return seen


def _is_cert_failure(output: str) -> bool:
    text = output.lower()
    return any(token in text for token in _CERT_FAIL)


def _run_logged(cmd: list[str]) -> tuple[int, str]:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **process_manager.creation_options(),
    )
    chunks: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        logger.info("%s", line.rstrip())
        chunks.append(line)
    return process.wait(), "".join(chunks)


def _require_cookies(settings: Settings) -> None:
    if not settings.bili_cookies.is_file():
        raise Yt2BiliError(
            f"未找到 B 站 Cookie：{settings.bili_cookies}\n"
            "请先运行：python -m yt2bili login"
        )


def _parse_bv(text: str) -> str:
    matches = _BV_RE.findall(text)
    return matches[-1] if matches else ""


def _latest_release() -> dict:
    raw = _http_bytes(_GITHUB_LATEST, accept="application/vnd.github+json")
    return json.loads(raw.decode("utf-8"))


def _select_asset(release: dict) -> tuple[str, str]:
    assets = release.get("assets") or []
    for needle in _asset_needles():
        for asset in assets:
            name = asset.get("name") or ""
            if needle in name:
                return name, asset["browser_download_url"]
    names = ", ".join(a.get("name", "") for a in assets)
    raise Yt2BiliError(
        f"GitHub Release 里没有匹配当前系统的 biliupR 包。可用文件：{names}"
    )


def _asset_needles() -> list[str]:
    machine = platform.machine().lower()
    arm = machine in {"arm64", "aarch64"}
    if sys.platform == "win32":
        return ["x86_64-windows.zip"]
    if sys.platform == "darwin":
        return (
            ["aarch64-macos.tar.xz", "x86_64-macos.tar.xz"]
            if arm
            else ["x86_64-macos.tar.xz"]
        )
    if arm:
        return ["aarch64-linux.tar.xz", "x86_64-linux.tar.xz"]
    return ["x86_64-linux.tar.xz"]


def _extract_biliup(data: bytes, asset_name: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    names = ("biliup.exe", "biliup")
    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = _find_member(zf.namelist(), names)
            dest.write_bytes(zf.read(member))
        return
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        member_name = _find_member(tf.getnames(), names)
        extracted = tf.extractfile(member_name)
        if extracted is None:
            raise Yt2BiliError("解压 biliup 失败。")
        dest.write_bytes(extracted.read())


def _find_member(names: list[str], wanted: tuple[str, ...]) -> str:
    for name in names:
        if Path(name).name in wanted and not name.endswith("/"):
            return name
    raise Yt2BiliError(f"压缩包里没有找到 {wanted}。")


def _http_bytes(url: str, accept: str | None = None) -> bytes:
    headers = {"User-Agent": "yt2bili"}
    if accept:
        headers["Accept"] = accept
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=120) as resp:
            return resp.read()
    except Exception as exc:
        raise Yt2BiliError(f"下载失败 {url}：{exc}") from exc
