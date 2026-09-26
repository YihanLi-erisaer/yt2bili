"""Experimental AcFun web-session publisher. No credentials enter task records."""
from __future__ import annotations

import http.cookiejar
import hashlib
import json
import math
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError

from yt2bili import events, publications
from yt2bili.db import _now
from yt2bili.exceptions import Yt2BiliError


class AuthRequired(Yt2BiliError): pass
class RateLimited(Yt2BiliError): pass
class UnknownResult(Yt2BiliError): pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuthRequired("AcFun 登录已跳转，请重新扫码。")


class WebClient:
    """A small HTTPS client with an explicit destination allowlist and cookie jar."""
    HOSTS = {"scan.acfun.cn", "www.acfun.cn", "member.acfun.cn", "upload.kuaishouzt.com"}

    def __init__(self, cookies=None):
        self.jar = http.cookiejar.CookieJar()
        for item in cookies or []:
            if item.get("domain", "").lstrip(".") not in {"acfun.cn", "scan.acfun.cn", "www.acfun.cn", "member.acfun.cn"}:
                continue
            self.jar.set_cookie(http.cookiejar.Cookie(0, item["name"], item["value"], None, False,
                item["domain"], True, item["domain"].startswith("."), item.get("path", "/"), True,
                False, None, False, None, None, {}, False))
        self.opener = build_opener(HTTPSHandler(), HTTPCookieProcessor(self.jar), _NoRedirect())

    def cookies(self):
        return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                for c in self.jar if c.domain.lstrip(".") in {"acfun.cn", "scan.acfun.cn", "www.acfun.cn", "member.acfun.cn"}]

    def request(self, method, url, data=None, raw=None, code=0):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in self.HOSTS or parsed.username or parsed.password or parsed.port or parsed.fragment:
            raise Yt2BiliError("AcFun 请求目标未获准。")
        body = raw if raw is not None else (urlencode(data).encode() if data is not None else None)
        origin = "https://www.acfun.cn" if parsed.hostname in {"scan.acfun.cn", "www.acfun.cn"} else "https://member.acfun.cn"
        headers = {"User-Agent": "Mozilla/5.0 StarDazz/0.2", "Accept": "application/json, text/plain, */*",
                   "Origin": origin, "Referer": origin + ("/login" if parsed.hostname == "scan.acfun.cn" else "/upload-video")}
        if body is not None: headers["Content-Type"] = "application/octet-stream" if raw is not None else "application/x-www-form-urlencoded"
        # The upload CDN must never receive AcFun session cookies.
        if parsed.hostname == "upload.kuaishouzt.com":
            opener = build_opener(HTTPSHandler(), _NoRedirect())
        else:
            opener = self.opener
        try:
            with opener.open(Request(url, data=body, headers=headers, method=method), timeout=60) as response:
                if response.status != 200:
                    raise Yt2BiliError("AcFun 请求未成功，已停止操作。")
                contents = response.read(2_000_001)
                if len(contents) > 2_000_000:
                    raise Yt2BiliError("AcFun 响应超出安全限制。")
                payload = json.loads(contents)
        except HTTPError as exc:
            if exc.code in (401, 403): raise AuthRequired("AcFun 登录已失效，请重新扫码。") from exc
            if exc.code == 429: raise RateLimited("AcFun 限流，请稍后手动恢复。") from exc
            raise Yt2BiliError(f"AcFun HTTP {exc.code}，已停止操作。") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise UnknownResult("AcFun 网络或响应不确定，请核对投稿状态。") from exc
        if not isinstance(payload, dict) or payload.get("result") != code:
            result_code = payload.get("result") if isinstance(payload, dict) else "invalid"
            raise Yt2BiliError(f"AcFun 业务响应失败（代码 {result_code}）。")
        return payload


class AcfunService:
    def __init__(self, store, config, emit, client_factory=WebClient):
        self.store, self.config, self.emit = store, config, emit
        self.client_factory = client_factory
        self.qr = None

    def _credential(self, value=...):
        vault = self.config.vault
        if not hasattr(vault, "get_password"):
            import keyring
            vault = keyring
        try:
            if value is not ...:
                if value is None:
                    if vault.get_password(self.config.vault_service, "acfun_cookies"):
                        vault.delete_password(self.config.vault_service, "acfun_cookies")
                else: vault.set_password(self.config.vault_service, "acfun_cookies", json.dumps(value))
            saved = vault.get_password(self.config.vault_service, "acfun_cookies")
            return json.loads(saved) if saved else []
        except Exception as exc:
            raise Yt2BiliError("无法访问系统凭据库中的 AcFun 登录态。") from exc

    def account(self):
        with self.store._lock:
            row = self.store._conn.execute("SELECT * FROM acfun_accounts WHERE lifecycle='active'").fetchone()
            return dict(row) if row else None

    def _client(self): return self.client_factory(self._credential())

    def _identity(self, client):
        result = client.request("POST", "https://www.acfun.cn/rest/pc-direct/user/personalInfo", data={})
        info = result.get("info")
        if not isinstance(info, dict) or not re.fullmatch(r"[1-9][0-9]*", str(info.get("userId", ""))):
            raise AuthRequired("AcFun 未返回可验证的账号 ID。")
        return str(info["userId"]), str(info.get("name") or info.get("userName") or info["userId"])

    def status(self, verify=False):
        account = self.account()
        error = ""
        if verify and account:
            try:
                user_id, _ = self._identity(self._client())
                if user_id != account["user_id"]: raise AuthRequired("AcFun 登录账号与绑定账号不一致。")
                with self.store.transaction() as db:
                    db.execute("UPDATE acfun_accounts SET auth_state='valid' WHERE account_id=?", (account["account_id"],))
                account = self.account()
            except Yt2BiliError as exc:
                error = str(exc)
                with self.store.transaction() as db:
                    db.execute("UPDATE acfun_accounts SET auth_state='expired' WHERE account_id=?", (account["account_id"],))
                account = self.account()
        enabled = self.config.values.get("acfun_experimental_enabled", False)
        return {"account": account, "can_sync": bool(enabled and account and account["auth_state"] == "valid"),
                "capabilities": {"auto_publish": False, "experimental": True}, "enabled": enabled,
                "error": error or ("AcFun 实验性网页投稿尚未启用。" if not enabled else ""), "limit": 1}

    def start(self):
        self.qr = {"client": self.client_factory(), "created": time.monotonic(), "phase": "scan"}
        response = self.qr["client"].request("GET", "https://scan.acfun.cn/rest/pc-direct/qr/start?type=WEB_LOGIN")
        token, signature, image = response.get("qrLoginToken"), response.get("qrLoginSignature"), response.get("imageData")
        if not all(isinstance(v, str) and v for v in (token, signature, image)):
            self.qr = None
            raise Yt2BiliError("AcFun 二维码响应缺少必要字段。")
        self.qr.update(token=token, signature=signature)
        return {"status": "waiting", "qrcode": image, "expires_in_ms": min(int(response.get("expireTime") or 120000), 120000)}

    def poll(self):
        qr = self.qr
        if not qr: return {"status": "idle"}
        if time.monotonic() - qr["created"] > 120:
            self.qr = None
            return {"status": "expired"}
        path = "scanResult" if qr["phase"] == "scan" else "acceptResult"
        url = "https://scan.acfun.cn/rest/pc-direct/qr/" + path + "?" + urlencode({"qrLoginToken": qr["token"], "qrLoginSignature": qr["signature"]})
        try: result = qr["client"].request("GET", url)
        except UnknownResult: return {"status": "waiting" if qr["phase"] == "scan" else "scanned"}
        except Yt2BiliError as exc:
            if "100400002" in str(exc): self.qr = None; return {"status": "expired"}
            raise
        if qr["phase"] == "scan":
            qr["signature"] = str(result.get("qrLoginSignature") or qr["signature"])
            qr["phase"] = "accept"
            return {"status": "scanned"}
        user_id, nickname = self._identity(qr["client"])
        cookies = qr["client"].cookies()
        if not cookies: raise AuthRequired("扫码成功但未取得 AcFun 登录凭据。")
        with self.store.transaction() as db:
            old = self.account()
            if old and old["user_id"] != user_id:
                raise Yt2BiliError("扫码账号与已绑定的 AcFun 账号不一致，请先处理旧任务。")
            if old:
                db.execute("UPDATE acfun_accounts SET nickname=?,auth_state='valid',binding_revision=binding_revision+1 WHERE account_id=?", (nickname, old["account_id"]))
            else:
                archived = db.execute("SELECT account_id FROM acfun_accounts WHERE user_id=?", (user_id,)).fetchone()
                if archived:
                    db.execute("UPDATE acfun_accounts SET lifecycle='active',auth_state='valid',nickname=?,binding_revision=binding_revision+1 WHERE account_id=?", (nickname, archived[0]))
                else:
                    db.execute("INSERT INTO acfun_accounts(account_id,user_id,nickname) VALUES(?,?,?)", (str(uuid.uuid4()), user_id, nickname))
            self._credential(cookies)
        self.qr = None
        self.emit("acfun.auth.changed", {"account_id": self.account()["account_id"]})
        return {"status": "done", "account": self.account()}

    def cancel(self): self.qr = None; return {"status": "idle"}

    def clear(self):
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM task_publications WHERE platform='acfun' AND status IN ('uploading_media','creating')").fetchone():
                raise Yt2BiliError("AcFun 正在投稿，请等待结果。")
            self._credential(None)
            db.execute("UPDATE acfun_accounts SET auth_state='missing',binding_revision=binding_revision+1 WHERE lifecycle='active'")
        return self.status()

    def archive(self):
        with self.store.transaction() as db:
            account = self.account()
            if not account: return self.status()
            if db.execute("SELECT 1 FROM task_publications WHERE platform='acfun' AND account_id=? AND status NOT IN ('submitted','abandoned')", (account["account_id"],)).fetchone():
                raise Yt2BiliError("请先完成、核对或放弃该 AcFun 账号的投稿。")
            self._credential(None)
            db.execute("UPDATE acfun_accounts SET lifecycle='archived',auth_state='missing' WHERE account_id=?", (account["account_id"],))
        return self.status()

    def check(self, account_id=None, revision=None, auto=False):
        status = self.status(True)
        account = status["account"]
        if not status["can_sync"]: raise AuthRequired(status["error"] or "请先登录 AcFun。")
        if account_id and account_id != account["account_id"] or revision is not None and revision != account["binding_revision"]:
            raise Yt2BiliError("AcFun 账号状态已变化，请刷新新建任务窗口。")
        if auto: raise Yt2BiliError("AcFun 网页接入尚未验证自动投稿，请选择预览模式。")
        return account

    def resume(self):
        self.emit("acfun.queue.resume", {})
        return {"resumed": True}

    def validate_assets(self, item, snapshot):
        task = self.store.require(item.task_id)
        title = snapshot.get("title") or task.title_zh
        description = snapshot.get("description") or task.desc_zh
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 50: raise Yt2BiliError("AcFun 标题须为 1～50 字，请在预览中编辑。")
        if not isinstance(description, str) or len(description) > 1000: raise Yt2BiliError("AcFun 简介超过 1000 字，请在预览中编辑。")
        if not isinstance(snapshot.get("channel_id"), int) or snapshot["channel_id"] <= 0: raise Yt2BiliError("请选择 AcFun 分区。")
        if not isinstance(snapshot.get("tags"), list) or len(snapshot["tags"]) > 6 or any(not isinstance(t, str) or not t.strip() for t in snapshot["tags"]):
            raise Yt2BiliError("AcFun 标签最多 6 个。")
        if not Path(task.video_path).is_file() or not Path(task.cover_path).is_file(): raise Yt2BiliError("AcFun 投稿素材缺失。")

    def _attempt(self, publication_id, **values):
        with self.store.transaction() as db:
            row = db.execute("SELECT attempt_id FROM acfun_attempts WHERE publication_id=? ORDER BY started_at DESC LIMIT 1", (publication_id,)).fetchone()
            if row:
                db.execute("UPDATE acfun_attempts SET " + ",".join(k + "=?" for k in values) + " WHERE attempt_id=?", (*values.values(), row[0]))

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            while block := source.read(1024 * 1024):
                events.check_cancelled()
                digest.update(block)
        return digest.hexdigest()

    def _upload_blob(self, client, path, token, part_size, item):
        size = Path(path).stat().st_size
        if not 0 < part_size <= 128 * 1024 * 1024 or size <= 0: raise Yt2BiliError("AcFun 分片参数无效。")
        count = math.ceil(size / part_size)
        with open(path, "rb") as source:
            for index in range(count):
                events.check_cancelled()
                part = source.read(part_size)
                if not part: raise Yt2BiliError("AcFun 本地素材读取中断。")
                client.request("POST", "https://upload.kuaishouzt.com/api/upload/fragment?" + urlencode({"fragment_id": index, "upload_token": token}), raw=part, code=1)
                events.progress("uploading_media", force=True, percent=round((index + 1) * 100 / count, 1))
        client.request("POST", "https://upload.kuaishouzt.com/api/upload/complete?" + urlencode({"fragment_count": count, "upload_token": token}), raw=b"", code=1)

    def publish(self, item):
        p = publications.for_platform(self.store, item.task_id, "acfun")
        account = self.check(p["account_id"], auto=item.mode == "auto")
        task = self.store.require(item.task_id)
        snapshot = json.loads(p["snapshot"])
        if snapshot.get("user_id") != account["user_id"]:
            raise AuthRequired("AcFun 投稿快照账号与当前登录账号不一致。")
        self.validate_assets(item, snapshot)
        video_hash = self._sha256(task.video_path)
        cover_hash = self._sha256(task.cover_path)
        media_hash = hashlib.sha256((video_hash + ":" + cover_hash).encode()).hexdigest()
        request_hash = hashlib.sha256(json.dumps({"snapshot": snapshot, "url": task.url}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        client = self._client()
        with self.store.transaction() as db:
            previous = db.execute("SELECT 1 FROM acfun_attempts WHERE publication_id=? AND create_intent_at!='' AND phase!='resolved_not_submitted'", (p["publication_id"],)).fetchone()
            if previous: raise Yt2BiliError("此 AcFun 目标已有投稿意图，请先核对，禁止自动重发。")
            db.execute("INSERT INTO acfun_attempts(attempt_id,publication_id,phase,started_at,media_sha256,request_sha256) VALUES(?,?,'uploading',?,?,?)",
                       (str(uuid.uuid4()), p["publication_id"], _now(), media_hash, request_hash))
            publications.change(self.store, p["publication_id"], status="uploading_media", error="")
        publications.project(self.store, item.task_id)
        filename = Path(task.video_path).name
        token_data = client.request("POST", "https://member.acfun.cn/video/api/getKSCloudToken", {"fileName": filename, "size": Path(task.video_path).stat().st_size, "template": "1"})
        upload_id, token, config = token_data.get("taskId"), token_data.get("token"), token_data.get("uploadConfig")
        if not upload_id or not token or not isinstance(config, dict): raise Yt2BiliError("AcFun 未返回完整视频上传参数。")
        self._attempt(p["publication_id"], upload_task_id=str(upload_id))
        self._upload_blob(client, task.video_path, token, int(config.get("partSize") or 0), item)
        # A lost createVideo response is unknown media state. Never proceed to createDouga.
        self._attempt(p["publication_id"], phase="creating_video")
        try: video = client.request("POST", "https://member.acfun.cn/video/api/createVideo", {"videoKey": upload_id, "fileName": filename, "vodType": "ksCloud"})
        except UnknownResult as exc:
            publications.change(self.store, p["publication_id"], status="submission_unknown", retain_assets=1, error="AcFun 视频素材创建结果不确定。")
            raise UnknownResult("AcFun 视频素材创建结果不确定，请核对后手动处理。") from exc
        video_id = video.get("videoId")
        if not video_id:
            publications.change(self.store, p["publication_id"], status="submission_unknown", retain_assets=1, error="AcFun 视频素材响应缺少 videoId。")
            raise UnknownResult("AcFun 视频素材响应缺少 videoId，请核对后手动处理。")
        self._attempt(p["publication_id"], video_id=str(video_id), phase="cover")
        client.request("POST", "https://member.acfun.cn/video/api/uploadFinish", {"taskId": upload_id})
        cover = Path(task.cover_path)
        cover_token = client.request("POST", "https://member.acfun.cn/common/api/getQiniuToken", {"fileName": cover.name})
        info = cover_token.get("info")
        token = info.get("token") if isinstance(info, dict) else None
        if not token: raise Yt2BiliError("AcFun 未返回封面上传令牌。")
        self._upload_blob(client, cover, token, min(cover.stat().st_size, 8 * 1024 * 1024), item)
        cover_result = client.request("POST", "https://member.acfun.cn/common/api/getUrlAfterUpload", {"bizFlag": "web-douga-cover", "token": token})
        cover_url = cover_result.get("url")
        if not isinstance(cover_url, str) or urlsplit(cover_url).scheme != "https": raise Yt2BiliError("AcFun 未返回有效封面地址。")
        events.check_cancelled()
        data = {"title": snapshot.get("title") or task.title_zh, "description": snapshot.get("description") or task.desc_zh,
                "tagNames": json.dumps(snapshot["tags"], ensure_ascii=False), "creationType": 1,
                "channelId": snapshot["channel_id"], "coverUrl": cover_url,
                "videoInfos": json.dumps([{"videoId": video_id, "title": snapshot.get("title") or task.title_zh}], ensure_ascii=False),
                "originalLinkUrl": task.url, "originalDeclare": "0", "isJoinUpCollege": "0", "isSyncKs": "0"}
        with self.store.transaction():
            self._attempt(p["publication_id"], phase="creating", create_intent_at=_now())
            publications.change(self.store, p["publication_id"], status="creating")
        publications.project(self.store, item.task_id)
        try: result = client.request("POST", "https://member.acfun.cn/video/api/createDouga", data)
        except Yt2BiliError as exc:
            publications.change(self.store, p["publication_id"], status="submission_unknown", retain_assets=1, error="AcFun 创建作品结果待核对。")
            publications.project(self.store, item.task_id)
            raise UnknownResult("AcFun 创建作品响应不确定；请到稿件管理核对，禁止重发。") from exc
        douga_id = str(result.get("dougaId") or "")
        if not re.fullmatch(r"[1-9][0-9]*", douga_id):
            publications.change(self.store, p["publication_id"], status="submission_unknown", retain_assets=1, error="AcFun 响应缺少有效 AC 号，需核对。")
            raise UnknownResult("AcFun 响应缺少有效 AC 号，需核对。")
        self._attempt(p["publication_id"], phase="submitted", douga_id=douga_id)
        publications.change(self.store, p["publication_id"], status="submitted", remote_id="AC" + douga_id, error="")
        publications.project(self.store, item.task_id)
