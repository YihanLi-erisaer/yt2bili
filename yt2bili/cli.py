from __future__ import annotations

import argparse
import logging
import sys

from yt2bili import bili_upload, pipeline
from yt2bili.config import load_settings
from yt2bili.db import TaskStore
from yt2bili.exceptions import Yt2BiliError
from yt2bili.media import require_ffmpeg

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

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
        logger.info("ffmpeg 与 biliup 已就绪：%s", path)
        logger.info("下一步：把 .env.example 复制为 .env，填入 DEEPL_AUTH_KEY，然后运行 python -m yt2bili login")
        return 0
    if command == "login":
        bili_upload.login(settings)
        return 0
    if command == "renew":
        bili_upload.renew(settings)
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
        task = pipeline.run(
            settings,
            store,
            args.url,
            dry_run=args.dry_run,
            force=args.force,
        )
        _print_result(task, args.dry_run)
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
        description="将你有权转载的 YouTube 单条视频下载、翻译并投稿到 B 站。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="检查 ffmpeg，并下载 biliup 命令行到 bin/")
    sub.add_parser("login", help="B 站扫码登录，Cookie 写入 secrets/")
    sub.add_parser("renew", help="刷新 B 站登录态")
    sub.add_parser("list", help="列出本地任务")

    run_p = sub.add_parser("run", help="处理一条 YouTube 链接")
    run_p.add_argument("url", help="YouTube 视频链接")
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
    retry_p.add_argument("video_id", help="YouTube 视频 ID")
    retry_p.add_argument("--dry-run", action="store_true", help="续跑但不上传")
    return parser


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
