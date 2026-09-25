"""Desktop side of the official Douyin broker. No platform secrets on desktop."""
import http.client
import json
import ssl
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from yt2bili import events, publications
from yt2bili.exceptions import Yt2BiliError


class BrokerError(Yt2BiliError):
    pass


class AuthRequired(Yt2BiliError):
    pass


class RateLimited(Yt2BiliError):
    pass


def platform_error(message):
    if message.startswith("AUTH_REQUIRED:"): raise AuthRequired(message)
    if message.startswith("RATE_LIMITED:"): raise RateLimited(message)


class DouyinService:
    def __init__(self, store, config, emit):
        self.store, self.config, self.emit = store, config, emit

    def credential(self, value=None):
        vault = self.config.vault
        # CLI uses its own DeepL provider; Douyin still uses the OS vault.
        if not hasattr(vault, "get_password"):
            import keyring
            vault = keyring
        try:
            if value is not None:
                if not isinstance(value, str) or not 16 <= len(value) <= 4096:
                    raise Yt2BiliError("服务配对密钥须为 16～4096 个字符。")
                vault.set_password(self.config.vault_service, "douyin_broker", value)
            return vault.get_password(self.config.vault_service, "douyin_broker") or ""
        except Yt2BiliError: raise
        except Exception as exc:
            raise Yt2BiliError("无法使用系统凭据库保存抖音服务配对密钥。") from exc

    def request(self, method, path, data=None, file=None):
        base = urlsplit(self.config.values.get("douyin_broker_url", ""))
        if base.scheme != "https" or not base.hostname or base.username or base.query or base.fragment or base.path not in ("", "/"):
            raise BrokerError("请先配置已部署的 HTTPS 抖音授权服务地址（只填写域名和端口）。")
        secret = self.credential()
        if not secret: raise BrokerError("请先保存抖音服务配对密钥。")
        conn = http.client.HTTPSConnection(base.hostname, base.port or 443, timeout=120, context=ssl.create_default_context())
        try:
            body = json.dumps(data or {}).encode()
            headers = {"Authorization": "Bearer " + secret, "Content-Type": "application/json"}
            if file:
                with open(file, "rb") as source:
                    headers.update({"Content-Type": "application/octet-stream", "Content-Length": str(Path(file).stat().st_size)})
                    conn.request(method, path, body=source, headers=headers)
            else: conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            result = json.loads(response.read(1024 * 1024))
            if response.status >= 400:
                message = result.get("error", "抖音服务请求失败。")
                platform_error(message)
                raise BrokerError(message)
            return result
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise BrokerError("无法确认抖音服务响应，请检查连接；投稿结果需通过原记录核对。") from exc
        finally: conn.close()

    def account(self):
        with self.store._lock:
            row = self.store._conn.execute("SELECT * FROM douyin_accounts WHERE lifecycle='active'").fetchone()
            return dict(row) if row else None

    def status(self, verify=False):
        error = ""
        capabilities = {"auto_publish": False}
        if verify:
            try:
                remote = self.request("GET", "/v1/account")
                capabilities = remote.get("capabilities", capabilities)
                identity = remote.get("account")
                with self.store.transaction() as db:
                    old = self.account()
                    if identity:
                        if old and (old["client_key"], old["open_id"]) != (identity["client_key"], identity["open_id"]):
                            raise Yt2BiliError("授权账号与已绑定账号不一致，请先处理旧任务并归档旧账号。")
                        if not old:
                            archived = db.execute("SELECT account_id FROM douyin_accounts WHERE client_key=? AND open_id=?", (identity["client_key"], identity["open_id"])).fetchone()
                            if archived:
                                db.execute("UPDATE douyin_accounts SET lifecycle='active',auth_state='valid',binding_revision=binding_revision+1 WHERE account_id=?", (archived[0],))
                            else:
                                db.execute("INSERT INTO douyin_accounts(account_id,client_key,open_id,nickname) VALUES(?,?,?,?)", (str(uuid.uuid4()), identity["client_key"], identity["open_id"], identity.get("nickname", "抖音账号")))
                        else:
                            db.execute("UPDATE douyin_accounts SET auth_state='valid' WHERE account_id=?", (old["account_id"],))
                    elif old:
                        db.execute("UPDATE douyin_accounts SET auth_state='expired' WHERE account_id=?", (old["account_id"],))
            except Yt2BiliError as exc:
                error = str(exc)
                with self.store.transaction() as db:
                    db.execute("UPDATE douyin_accounts SET auth_state='unavailable' WHERE lifecycle='active'")
        account = self.account()
        if verify and account and account["auth_state"] == "valid":
            self.emit("douyin.auth.changed", {"account_id": account["account_id"]})
        return {"account": account, "configured": bool(self.config.values.get("douyin_broker_url")),
                "can_sync": bool(account and account["auth_state"] == "valid"), "capabilities": capabilities, "error": error, "limit": 1}

    def configure(self, url, pairing_key):
        base = urlsplit(url)
        if base.scheme != "https" or not base.hostname or base.username or base.password or base.query or base.fragment or base.path not in ("", "/"):
            raise Yt2BiliError("授权服务必须为 HTTPS 源地址。")
        if self.account(): raise Yt2BiliError("更换服务前须先归档抖音账号。")
        self.credential(pairing_key)
        self.config.update({"douyin_broker_url": url.rstrip("/")})
        return self.status(True)

    def start(self):
        result = self.request("POST", "/v1/auth/start")
        url = urlsplit(result.get("url", ""))
        if url.scheme != "https" or url.netloc != "open.douyin.com" or url.path != "/platform/oauth/connect/":
            raise BrokerError("授权服务返回了非官方授权地址，已阻止打开。")
        webbrowser.open(result["url"])
        return {"started": True}

    def clear(self):
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM task_publications WHERE platform='douyin' AND status IN ('uploading_media','creating')").fetchone():
                raise Yt2BiliError("抖音正在投稿，请等待结果。")
            self.request("POST", "/v1/auth/clear")
            db.execute("UPDATE douyin_accounts SET auth_state='missing',binding_revision=binding_revision+1 WHERE lifecycle='active'")
        return self.status()

    def archive(self):
        with self.store.transaction() as db:
            account = self.account()
            if not account: return self.status()
            if db.execute("SELECT 1 FROM task_publications WHERE platform='douyin' AND account_id=? AND status NOT IN ('submitted','abandoned')", (account["account_id"],)).fetchone():
                raise Yt2BiliError("请先完成、核对或放弃该抖音账号的所有未完成投稿。")
            self.request("POST", "/v1/auth/archive")
            db.execute("UPDATE douyin_accounts SET lifecycle='archived',auth_state='missing' WHERE account_id=?", (account["account_id"],))
        return self.status()

    def check(self, account_id=None, revision=None, auto=False):
        status = self.status(True)
        account = status["account"]
        if not status["can_sync"]: raise AuthRequired(status["error"] or "请先登录抖音。")
        if account_id and account_id != account["account_id"] or revision is not None and revision != account["binding_revision"]:
            raise Yt2BiliError("抖音账号状态已变化，请重新打开新建任务窗口。")
        if auto and not status["capabilities"].get("auto_publish"):
            raise Yt2BiliError("当前抖音服务尚未启用官方批准的自动发布能力，请选择预览模式。")
        return account

    def resume(self):
        self.request("POST", "/v1/resume")
        self.emit("douyin.queue.resume", {})
        return {"resumed": True}

    def publish(self, item):
        p = publications.for_platform(self.store, item.task_id, "douyin")
        account = self.check(p["account_id"], auto=item.mode == "auto")
        task = self.store.require(item.task_id)
        snap = json.loads(p["snapshot"])
        events.check_cancelled()
        self.validate_assets(item, snap.get("text", ""))
        endpoint = "/v1/publications/" + p["publication_id"]
        payload = {"source_video_id": task.video_id, "open_id": account["open_id"],
                   "client_key": account["client_key"], "text": snap["text"], "auto": item.mode == "auto"}
        result = self.request("POST", endpoint, payload)
        if result["status"] not in ("submitted", "submission_unknown"):
            with self.store.transaction():
                events.check_cancelled()
                current = publications.get(self.store, p["publication_id"])
                if current["status"] not in ("queued", "waiting"):
                    raise Yt2BiliError("抖音投稿状态已变化，未开始上传。")
                publications.change(self.store, p["publication_id"], status="uploading_media", error="")
            publications.project(self.store, item.task_id)
            self.request("PUT", endpoint + "/video", file=task.video_path)
            events.check_cancelled()
            self.request("PUT", endpoint + "/cover", file=task.cover_path)
            events.check_cancelled()
            publications.change(self.store, p["publication_id"], status="creating")
            publications.project(self.store, item.task_id)
            try:
                result = self.request("POST", endpoint + "/submit")
            except BrokerError:
                # GET recovers a committed receipt, never retries create.
                try: result = self.request("GET", endpoint)
                except BrokerError: result = {"status": "submission_unknown", "error": "服务响应丢失，请核对原投稿记录。"}
        state = result.get("status", "submission_unknown")
        if state not in ("submitted", "failed", "submission_unknown"):
            state = "submission_unknown"
        publications.change(self.store, p["publication_id"], status=state,
                            remote_id=result.get("remote_id", ""), error=result.get("error", ""))
        if state == "failed": platform_error(result.get("error", ""))

    def validate_assets(self, item, text):
        task = self.store.require(item.task_id)
        if not text.strip() or len(text) > 1000:
            raise Yt2BiliError("抖音文案必须为 1～1000 字。")
        if Path(task.video_path).stat().st_size > 4 * 1024**3:
            raise Yt2BiliError("抖音视频超出 4 GB 限制。")
        # Metadata comes from the shared validated source, without a second translation.
        if item.job.meta.duration and item.job.meta.duration > 900:
            raise Yt2BiliError("抖音视频不能超过 15 分钟；Bilibili 分支不受影响。")
