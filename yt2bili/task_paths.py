"""Validate a task's dedicated directory before writing or deleting assets."""
from pathlib import Path
from yt2bili.exceptions import Yt2BiliError


def validate_task_paths(task):
    if not task.work_dir or not task.work_root:
        raise Yt2BiliError("任务素材根目录未确认，请重新准备素材。")
    folder, root = Path(task.work_dir), Path(task.work_root)
    if not folder.is_absolute() or not root.is_absolute() or folder.name not in (task.task_id, task.video_id):
        raise Yt2BiliError("任务必须使用独立的素材目录。")
    if folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction()) or folder.resolve().parent != root.resolve():
        raise Yt2BiliError("任务素材目录不能跳转到其他位置。")
    for value in (task.video_path, task.cover_path):
        if value and folder.resolve() not in Path(value).resolve().parents:
            raise Yt2BiliError("素材文件不在本任务目录内。")
    return folder
