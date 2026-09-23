from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

from yt2bili import bili_upload, pipeline, youtube
from yt2bili.config import load_settings
from yt2bili.cli_settings import CLISettings
from yt2bili.desktop_service import DesktopService
from yt2bili.exceptions import Yt2BiliError
from yt2bili.media import require_ffmpeg
from yt2bili.paths import AppPaths
from yt2bili.process_manager import own_children

logger = logging.getLogger(__name__)


def main(argv=None):
    _configure_stdio()
    args = _build_parser().parse_args(_protect_retry_video_id(sys.argv[1:] if argv is None else list(argv)))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    pipeline.install_log_filter()
    service = None
    try:
        settings = load_settings()
        if args.command == "setup":
            require_ffmpeg()
            logger.info("biliup：%s；YouTube JS：%s", bili_upload.setup_biliup(settings), youtube.describe_js_runtimes(settings))
            return 0
        if args.command == "youtube-cookies":
            logger.info("已导出：%s", youtube.export_browser_cookies(settings, args.browser))
            return 0
        paths = AppPaths.default(args.data_dir or settings.root, settings.root)
        if args.data_dir:
            settings = replace(settings, work_dir=paths.root / "work", data_dir=paths.root / "data")
        with own_children():
            service = DesktopService(paths, lambda *args: None, config=CLISettings(paths, settings))
            try:
                return _dispatch(args, service)
            finally:
                service.close()
    except Yt2BiliError as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.error("已请求停止；在途投稿需结束后退出。")
        return 130


def _dispatch(args, service):
    if args.command == "accounts":
        if args.account_command == "list":
            for account in service.accounts.list(True):
                logger.info("%s UID=%s %s %s", account["account_id"], account["uid"], account["nickname"], account["lifecycle"])
        elif args.account_command == "archive":
            service.archive_account(args.account)
        elif args.account_command == "import":
            service.import_cookies(args.path, "bilibili", args.account)
        else:
            _login(service, None)
        return 0
    if args.command == "login":
        _login(service, args.account)
        return 0
    if args.command == "renew":
        service.renew(args.account)
        return 0
    if args.command == "list":
        for task in service.store.list_all():
            if args.account and task.account_id != args.account:
                continue
            logger.info("%s video=%s account=%s UID=%s %s %s", task.task_id, task.video_id,
                        task.account_id or "legacy_unbound", task.account_uid_snapshot, task.status, task.bv_id)
        return 0
    op = str(uuid.uuid4())
    if args.command == "run":
        result = service.create(args.url, op, args.account, "auto" if args.auto else "preview")
        task_id = result["task_id"]
        if not result["created"]:
            logger.info("已存在同账号任务：%s；不会重复执行。", task_id)
            return 0
    else:
        task_id = service.task(args.task_id).task_id
        getattr(service, args.command)(task_id, op)
    logger.info("任务 %s 已加入队列；取消请按 Ctrl+C。", task_id)
    while (service.store.get_job(task_id) or {}).get("execution_state") in ("queued", "running", "waiting"):
        time.sleep(.2)
    task = service.task(task_id)
    logger.info("%s %s %s", task.task_id, task.status, task.bv_id or task.error)
    return 0 if task.status in ("ready", "submitted") else 1


def _login(service, account_id):
    temporary = service.paths.root / "secrets" / ("login-" + str(uuid.uuid4()) + ".json")
    try:
        bili_upload.login(replace(service.config.build(), bili_cookies=temporary))
        result = service.accounts.bind(json.loads(temporary.read_text(encoding="utf-8")), account_id)
        logger.info("账号已绑定：%s UID=%s", result["account_id"], result["uid"])
    finally:
        temporary.unlink(missing_ok=True)


def _build_parser():
    parser = argparse.ArgumentParser(prog="yt2bili", description="单链接、显式账号的本地投稿工具。")
    parser.add_argument("--data-dir", help="账号与任务数据目录；默认项目目录")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    yt = sub.add_parser("youtube-cookies")
    yt.add_argument("--browser", default=None)
    sub.add_parser("list").add_argument("--account")
    for command in ("login", "renew"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--account", required=True, help="原账号 account_id；新增用 accounts add")
    accounts = sub.add_parser("accounts").add_subparsers(dest="account_command", required=True)
    accounts.add_parser("list")
    accounts.add_parser("add")
    archive = accounts.add_parser("archive")
    archive.add_argument("--account", required=True)
    imp = accounts.add_parser("import")
    imp.add_argument("path")
    imp.add_argument("--account", help="不传时新增或恢复归档 UID")
    run = sub.add_parser("run", help="一次只接收一个 YouTube 视频链接")
    run.add_argument("url")
    run.add_argument("--account", required=True)
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--auto", action="store_true", help="素材准备好后自动投稿")
    mode.add_argument("--dry-run", action="store_true", help="准备素材等待确认（默认）")
    for command in ("retry", "submit", "repair"):
        cmd = sub.add_parser(command)
        cmd.add_argument("task_id", help="任务 UUID；兼容无歧义的旧视频 ID")
        if command == "retry":
            cmd.add_argument("--dry-run", action="store_true", help="继续后始终进入预览")
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


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
