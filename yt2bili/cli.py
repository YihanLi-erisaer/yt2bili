from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from yt2bili import bili_upload, pipeline, youtube
from yt2bili.config import load_settings
from yt2bili.db import TaskStore
from yt2bili.exceptions import Yt2BiliError
from yt2bili.media import require_ffmpeg

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = _build_parser()
    raw = sys.argv[1:] if argv is None else list(argv)
    args = parser.parse_args(_protect_retry_video_id(raw))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    pipeline.install_log_filter()

    settings = load_settings()
    store = TaskStore(settings.data_dir / "tasks.sqlite")
    try:
        return _dispatch(args, settings, store)
    except Yt2BiliError as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.error("已中断。")
        return 130
    finally:
        store.close()


def _dispatch(args: argparse.Namespace, settings, store: TaskStore) -> int:
    command = args.command
    if command == "setup":
        require_ffmpeg()
        path = bili_upload.setup_biliup(settings)
        js = youtube.describe_js_runtimes(settings)
        logger.info("ffmpeg 与 biliup 已就绪：%s", path)
        if js:
            logger.info("YouTube JS 运行时：%s", js)
        else:
            logger.warning(
                "未找到 Deno 或 Node.js，YouTube 解析可能被拦截。"
                "请安装 https://nodejs.org 或把 deno.exe 放到 bin\\"
            )
        logger.info("下一步：把 .env.example 复制为 .env，填入 DEEPL_AUTH_KEY，然后运行 python -m yt2bili login")
        return 0
    if command == "login":
        bili_upload.login(settings)
        return 0
    if command == "renew":
        bili_upload.renew(settings)
        return 0
    if command == "youtube-cookies":
        path = youtube.export_browser_cookies(settings, args.browser)
        logger.info("以后下载会自动使用：%s", path)
        return 0
    if command == "list":
        tasks = store.list_all()
        if not tasks:
            logger.info("还没有任务。")
            return 0
        for task in tasks:
            extra = f" bv={task.bv_id}" if task.bv_id else ""
            err = f" error={task.error}" if task.error else ""
            title = task.title_zh or task.title_orig or ""
            logger.info("%s  %s  %s%s%s", task.video_id, task.status, title, extra, err)
        return 0
    if command == "run":
        urls = list(args.urls or [])
        if args.file:
            urls.extend(_read_url_file(args.file))
        if not urls:
            raise Yt2BiliError("请提供至少一个 YouTube 链接，或用 --file 指定列表文件。")
        jobs = args.jobs if args.jobs is not None else settings.download_jobs
        if len(urls) == 1 and not args.file:
            task = pipeline.run(
                settings,
                store,
                urls[0],
                dry_run=args.dry_run,
                force=args.force,
            )
            _print_result(task, args.dry_run)
            return 0
        results, failures = pipeline.run_many(
            settings,
            store,
            urls,
            dry_run=args.dry_run,
            force=args.force,
            jobs=jobs,
        )
        for task in results:
            _print_result(task, args.dry_run)
        if failures:
            logger.error("%s 条失败。", len(failures))
            return 1
        return 0
    if command == "retry":
        task = pipeline.retry(
            settings,
            store,
            args.video_id,
            dry_run=args.dry_run,
        )
        _print_result(task, args.dry_run)
        return 0
    if command == "repair":
        for video_id in args.video_ids:
            path = pipeline.repair(settings, store, video_id, redownload=args.redownload, encoder=args.encoder, max_size_gb=args.max_size_gb)
            logger.info("修复完成（未上传）：%s", path)
        return 0
    raise Yt2BiliError(f"未知命令：{command}")


def _print_result(task, dry_run: bool) -> None:
    if dry_run:
        logger.info("dry-run 完成：%s", task.video_id)
        return
    if task.status == "submitted":
        logger.info("已提交：%s %s", task.video_id, task.bv_id or "(无 BV 号)")
    else:
        logger.info("当前状态：%s %s", task.video_id, task.status)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yt2bili",
        description="将你有权转载的 YouTube 视频下载、翻译并投稿到 B 站。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="检查 ffmpeg，并下载 biliup 命令行到 bin/")
    sub.add_parser("login", help="B 站扫码登录，Cookie 写入 secrets/")
    sub.add_parser("renew", help="刷新 B 站登录态")
    sub.add_parser("list", help="列出本地任务")

    yt_ck = sub.add_parser(
        "youtube-cookies",
        help="从已登录的浏览器导出 YouTube cookies（导出前请完全退出浏览器）",
    )
    yt_ck.add_argument(
        "--browser",
        default=None,
        help="chrome / edge / firefox，默认按顺序尝试",
    )

    run_p = sub.add_parser("run", help="处理一条或多条 YouTube 链接")
    run_p.add_argument("urls", nargs="*", help="YouTube 视频链接（可多条）")
    run_p.add_argument(
        "-f",
        "--file",
        metavar="PATH",
        help="从文本文件读取链接，每行一条；空行和 # 开头的行会忽略",
    )
    run_p.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help="兼容旧参数；当前下载、校验、上传三个队列均固定单路，此参数不再增加并发",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="只下载、处理封面并翻译，不上传",
    )
    run_p.add_argument(
        "--force",
        action="store_true",
        help="忽略已完成记录，重新下载并处理",
    )

    retry_p = sub.add_parser("retry", help="从失败步骤继续某个 video_id")
    retry_p.add_argument(
        "video_id",
        help="YouTube 视频 ID（以 - 开头时也可直接写，如 retry -GiIT0fNvW8）",
    )
    retry_p.add_argument("--dry-run", action="store_true", help="续跑但不上传")
    repair_p = sub.add_parser("repair", help="修复本地投稿文件，不重复投稿；可用于已提交但平台转码失败的任务")
    repair_p.add_argument("video_ids", nargs="+", help="一个或多个已有任务的 YouTube 视频 ID")
    repair_p.add_argument("--redownload", action="store_true", help="保留旧文件到 rejected/，重新下载完整源文件")
    repair_p.add_argument("--encoder", choices=("libx264", "h264_nvenc"), default="libx264", help="仅非 MP4 转换时使用；完整 MP4 直接复用，不转码")
    repair_p.add_argument("--max-size-gb", type=float, default=None, help="文件大小上限（十进制 GB）；MP4 超限则报错，不自动压缩，非 MP4 转换时限制码率")
    return parser


def _protect_retry_video_id(argv: list[str]) -> list[str]:
    """Keep YouTube IDs that start with '-' from being parsed as flags."""
    if "retry" not in argv:
        return argv
    idx = argv.index("retry")
    rest = argv[idx + 1 :]
    if "--" in rest:
        return argv
    flags = {"-h", "--help", "--dry-run"}
    video_id: str | None = None
    options: list[str] = []
    extras: list[str] = []
    for token in rest:
        if token in flags:
            options.append(token)
        elif video_id is None:
            video_id = token
        else:
            extras.append(token)
    out = argv[:idx] + ["retry"] + options
    if video_id is not None:
        if video_id.startswith("-"):
            out.append("--")
        out.append(video_id)
    out.extend(extras)
    return out


def _read_url_file(path: str) -> list[str]:
    file_path = Path(path)
    if not file_path.is_file():
        raise Yt2BiliError(f"找不到链接列表文件：{file_path}")
    urls: list[str] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        urls.append(text)
    if not urls:
        raise Yt2BiliError(f"{file_path} 里没有有效链接。")
    return urls


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
