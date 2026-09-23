"""CLI .env configuration, snapshotted into the shared task service."""
from dataclasses import replace
from pathlib import Path
from yt2bili.desktop_settings import DesktopSettings


class CLISettings(DesktopSettings):
    def __init__(self, paths, settings):
        self.base = settings
        super().__init__(paths, vault=self)
        if not self.path.exists():
            self.values.update(work_dir=str(settings.work_dir), bili_tid=settings.bili_tid,
                bili_tags=settings.bili_tags, bili_line=settings.bili_line,
                upload_gap_seconds=settings.upload_gap_seconds)

    def key(self):
        return self.base.deepl_auth_key

    def build(self, snapshot=None):
        value = snapshot or self.values
        return replace(self.base, root=self.paths.root, data_dir=self.paths.root / "data",
                       work_dir=Path(value["work_dir"]), bili_tid=value["bili_tid"],
                       bili_tags=value["bili_tags"], bili_line=value["bili_line"],
                       upload_gap_seconds=value["upload_gap_seconds"])
