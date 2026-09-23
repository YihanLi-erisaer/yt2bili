"""Five-account registry and atomic, versioned credential publication."""
from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from pathlib import Path

from yt2bili import desktop_auth
from yt2bili.db import _now
from yt2bili.desktop_settings import atomic_json
from yt2bili.exceptions import Yt2BiliError
from yt2bili.locking import account_guard


class AccountService:
    def __init__(self, root, store, emit):
        self.root, self.store, self.emit = Path(root), store, emit

    def public(self, account):
        return {k: v for k, v in account.items() if k != "credential_ref"}

    def list(self, archived=False):
        return [self.public(a) for a in self.store.accounts(archived)]

    def read(self, account_id):
        account = self.store.account(account_id)
        ref = account["credential_ref"]
        if not ref:
            raise Yt2BiliError("登录失效，请重新登录该账号。")
        path = Path(ref).resolve()
        allowed = (self.root / "secrets/bilibili" / account_id).resolve()
        if allowed not in path.parents or not path.is_file():
            raise Yt2BiliError("登录凭据不存在或路径无效，请重新登录。")
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Yt2BiliError("登录文件无效，请重新登录。") from exc
        if desktop_auth.credential_identity(info) != account["uid"]:
            raise Yt2BiliError("凭据 UID 与绑定账号不一致。")
        return account, info

    def bind(self, info, account_id=None, verify=True, locked=False, verified_identity=None):
        uid = desktop_auth.credential_identity(info)
        identity = verified_identity or (desktop_auth.verify_credentials(info) if verify else {"uid": uid, "nickname": ""})
        if identity["uid"] != uid:
            raise Yt2BiliError("在线 UID 与凭据不一致。")
        verified = verify or verified_identity is not None
        with nullcontext() if locked else account_guard(uid):
            target = self.store.account(account_id, active=False) if account_id else None
            if target and target["uid"] != uid:
                raise Yt2BiliError("误扫了其他账号，请使用原 UID 重新登录。")
            same = next((a for a in self.store.accounts(True) if a["uid"] == uid), None)
            if not target and same:
                if same["lifecycle"] == "active":
                    raise Yt2BiliError("该 UID 已绑定，请在原账号上重新登录。")
                target = same
            account_id = target["account_id"] if target else str(uuid.uuid4())
            file = self.root / "secrets/bilibili" / account_id / "credentials" / (str(uuid.uuid4()) + ".json")
            atomic_json(file, info)
            try:
                with self.store.transaction() as conn:
                    current = self.store.account(account_id, active=False) if target else None
                    if current and current["lifecycle"] == "active":
                        slot = current["slot"]
                    else:
                        used = {a["slot"] for a in self.store.accounts()}
                        slot = next((n for n in range(1, 6) if n not in used), None)
                        if slot is None:
                            raise Yt2BiliError("最多支持 5 个 Bilibili 账号，请先归档空闲账号。")
                    if not current:
                        conn.execute("INSERT INTO bilibili_accounts(account_id,uid,slot,created_at,updated_at) VALUES(?,?,?,?,?)",
                                     (account_id, uid, slot, _now(), _now()))
                    self.store.update_account(account_id, slot=slot, lifecycle="active", credential_ref=str(file),
                        credential_revision=(current["credential_revision"] if current else 0) + 1,
                        nickname=identity["nickname"] or (current["nickname"] if current else ""),
                        auth_state="valid" if verified else "unverified", verified_at=_now() if verified else None)
            except BaseException:
                file.unlink(missing_ok=True)
                raise
            result = self.public(self.store.account(account_id))
            self.emit("accounts.changed", {"account_id": account_id})
            return result

    def verify(self, account_id):
        with account_guard(self.store.account(account_id)["uid"]):
            account, info = self.read(account_id)
            try:
                identity = desktop_auth.verify_credentials(info)
            except Yt2BiliError as exc:
                self.store.update_account(account_id, auth_state="expired" if "失效" in str(exc) else "unavailable")
                raise
            self.store.update_account(account_id, auth_state="valid", nickname=identity["nickname"], verified_at=_now())
        self.emit("accounts.changed", {"account_id": account_id})
        return self.public(self.store.account(account_id))

    def clear(self, account_id):
        with account_guard(self.store.account(account_id)["uid"]):
            self.store.update_account(account_id, credential_ref=None, auth_state="missing", verified_at=None)
            # Hold the UID lock until old versions are gone.
            folder = self.root / "secrets/bilibili" / account_id / "credentials"
            for file in folder.glob("*.json"):
                file.unlink(missing_ok=True)
        self.emit("accounts.changed", {"account_id": account_id})
        return {"cleared": True}

    def archive(self, account_id):
        with account_guard(self.store.account(account_id)["uid"]):
            with self.store.transaction():
                if any(t.account_id == account_id and t.status != "submitted" for t in self.store.list_all()):
                    raise Yt2BiliError("账号仍有未完成任务，不能归档。")
                self.store.update_account(account_id, lifecycle="archived", slot=None)
        self.emit("accounts.changed", {"account_id": account_id})
        return {"archived": True}

    def rename(self, account_id, remark):
        if not isinstance(remark, str) or len(remark) > 80:
            raise Yt2BiliError("备注不能超过 80 字。")
        self.store.update_account(account_id, remark=remark.strip())
        self.emit("accounts.changed", {"account_id": account_id})
        return self.public(self.store.account(account_id))
