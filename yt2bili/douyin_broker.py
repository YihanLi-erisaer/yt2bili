"""Single-owner official API broker. Run behind an HTTPS reverse proxy, never public HTTP.

python -m yt2bili.douyin_broker --data-dir /srv/yt2bili-douyin
Secrets are environment variables; tokens are encrypted at rest with Fernet.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit


class Rejected(ValueError):
    """An explicit platform rejection, not an uncertain network outcome."""
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class OfficialAPI:
    def call(self, path, *, token="", query=None, data=None, form=False, binary=None, field="video"):
        headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
        if token: headers["access-token"] = token
        if binary is not None:
            boundary = secrets.token_hex(20)
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="asset.{"jpg" if field == "image" else "mp4"}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                    + binary + f"\r\n--{boundary}--\r\n".encode())
        else:
            body = urlencode(data or {}).encode() if form else json.dumps(data or {}).encode()
        conn = http.client.HTTPSConnection("open.douyin.com", timeout=120)
        try:
            conn.request("POST", path + ("?" + urlencode(query) if query else ""), body, headers)
            response = conn.getresponse()
            raw = json.loads(response.read(1024 * 1024))
            if response.status != 200: raise OSError("Platform HTTP response unavailable")
            result = raw.get("data")
            if not isinstance(result, dict): raise OSError("Malformed platform response")
            for part in (result, raw.get("extra", {})):
                if str(part.get("error_code", 0)) != "0":
                    # Never echo tokens or untrusted full platform responses.
                    code = str(part["error_code"])
                    if not code.isdecimal(): raise OSError("Malformed platform error code")
                    kind = "AUTH_REQUIRED" if code in {"28001003", "28001008", "10010"} else "RATE_LIMITED" if code == "28003017" else "REJECTED"
                    raise Rejected(kind + ": 抖音接口错误码 " + code[:24], code=code)
            return result
        finally: conn.close()

    def upload(self, token, open_id, file, cover=False):
        prefix = "/api/douyin/v1/video/"
        query = {"open_id": open_id}
        size = Path(file).stat().st_size
        with open(file, "rb") as source:
            if cover or size <= 50 * 1024**2:
                result = self.call(prefix + ("upload_image/" if cover else "upload_video/"), token=token,
                                   query=query, binary=source.read(), field="image" if cover else "video")
            else:
                upload = self.call(prefix + "init_video_part_upload/", token=token, query=query)["upload_id"]
                query["upload_id"] = upload
                # Balanced >=5 MB parts avoid an undersized final part.
                count = math.ceil(size / (20 * 1024**2))
                chunk = math.ceil(size / count)
                for index in range(1, count + 1):
                    self.call(prefix + "upload_video_part/", token=token, query={**query, "part_number": index}, binary=source.read(chunk))
                result = self.call(prefix + "complete_video_part_upload/", token=token, query=query)
        return result["image" if cover else "video"]["image_id" if cover else "video_id"]


class Broker:
    def __init__(self, root, client_key, client_secret, callback, pairing_key, encryption_key, auto=False, api=None):
        from cryptography.fernet import Fernet
        if len(pairing_key) < 32: raise ValueError("Pairing key must be at least 32 characters")
        url = urlsplit(callback)
        if url.scheme != "https" or not url.hostname or url.path != "/oauth/callback" or url.query or url.fragment or url.username:
            raise ValueError("Callback must be the registered HTTPS /oauth/callback URL")
        self.root = Path(root).resolve()
        if self.root == self.root.parent: raise ValueError("Use a dedicated service data directory, not a drive root")
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt": self.root.chmod(0o700)
        self.key, self.secret, self.callback, self.pairing, self.auto = client_key, client_secret, callback, pairing_key, auto
        self.crypt, self.api, self.lock = Fernet(encryption_key), api or OfficialAPI(), threading.RLock()
        self.db = sqlite3.connect(self.root / "broker.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS publications(
            id TEXT PRIMARY KEY, open_id TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL,
            hash TEXT NOT NULL, status TEXT NOT NULL, media_id TEXT NOT NULL DEFAULT '',
            cover_id TEXT NOT NULL DEFAULT '', remote_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            UNIQUE(open_id,source))""")
        self.db.execute("UPDATE publications SET status='submission_unknown',error='服务上次在创建作品时中断，请核对。' WHERE status='creating'")
        self.db.commit()
        existing_key = self.value("client_key")
        if existing_key and existing_key != self.key: raise ValueError("Use a new data directory for a different application")
        self.save("client_key", self.key)

    def value(self, name):
        row = self.db.execute("SELECT value FROM config WHERE key=?", (name,)).fetchone()
        return row[0] if row else ""

    def save(self, name, value):
        self.db.execute("INSERT OR REPLACE INTO config VALUES(?,?)", (name, value))
        self.db.commit()

    def token(self):
        stored = self.value("token")
        if not stored: raise Rejected("抖音尚未授权或授权已清除。")
        value = json.loads(self.crypt.decrypt(stored.encode()))
        if value["expires_at"] < time.time() + 120:
            refreshed = self.api.call("/oauth/refresh_token/", data={"client_key": self.key,
                "grant_type": "refresh_token", "refresh_token": value["refresh_token"]}, form=True)
            if refreshed["open_id"] != value["open_id"]: raise Rejected("刷新令牌的账号身份不一致。")
            value.update(refreshed, expires_at=time.time() + int(refreshed["expires_in"]))
            self.save("token", self.crypt.encrypt(json.dumps(value).encode()).decode())
        return value

    def auth_start(self):
        state = secrets.token_urlsafe(32)
        self.save("oauth", json.dumps({"state": state, "expires": time.time() + 600}))
        return {"url": "https://open.douyin.com/platform/oauth/connect/?" + urlencode({"client_key": self.key,
            "response_type": "code", "scope": "video.create.bind", "redirect_uri": self.callback, "state": state})}

    def callback_code(self, code, state):
        pending = json.loads(self.value("oauth") or "{}")
        if not pending or pending["expires"] < time.time() or not hmac.compare_digest(pending["state"], state):
            raise Rejected("授权会话已过期或取消，请重新开始。")
        self.save("oauth", "")  # consume before exchanging the one-use code
        result = self.api.call("/oauth/access_token/", form=True, data={"client_key": self.key,
            "client_secret": self.secret, "grant_type": "authorization_code", "code": code})
        if "video.create.bind" not in result.get("scope", "").split(","): raise Rejected("未授予发布权限。")
        old = self.value("identity")
        if old and old != result["open_id"]: raise Rejected("请授权原抖音账号；更换账号须先在桌面归档。")
        self.save("identity", result["open_id"])
        result["expires_at"] = time.time() + int(result["expires_in"])
        self.save("token", self.crypt.encrypt(json.dumps(result).encode()).decode())
        return {"message": "授权完成。请返回桌面点击检查授权状态。"}

    def publication(self, identity):
        row = self.db.execute("SELECT * FROM publications WHERE id=?", (identity,)).fetchone()
        if not row: raise Rejected("投稿记录不存在。")
        return dict(row)

    def receipt(self, p):
        return {k: p[k] for k in ("status", "remote_id", "error")}

    def update(self, identity, **values):
        self.db.execute("UPDATE publications SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), identity))
        self.db.commit()

    def register(self, identity, data):
        if self.value("rate_limited"): raise Rejected("RATE_LIMITED: 抖音限流暂停，请明确恢复队列。")
        token = self.token()
        if data.get("client_key") != self.key or data.get("open_id") != token["open_id"]: raise Rejected("目标抖音身份不一致。")
        if data.get("auto") and not self.auto: raise Rejected("未启用官方批准的自动发布场景。")
        if not isinstance(data.get("text"), str) or not 1 <= len(data["text"].strip()) <= 1000: raise Rejected("文案须为 1～1000 字。")
        if not re.fullmatch(r"[\w-]{11}", data.get("source_video_id", ""), flags=re.ASCII): raise Rejected("源视频 ID 无效。")
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        existing = self.db.execute("SELECT * FROM publications WHERE id=?", (identity,)).fetchone()
        if existing:
            if existing["open_id"] != token["open_id"] or existing["source"] != data["source_video_id"]: raise Rejected("投稿身份不可改变。")
            if existing["hash"] != digest:
                if existing["status"] not in ("ready", "failed"): raise Rejected("已开始提交的文案不可改变。")
                self.update(identity, hash=digest, payload=payload, status="ready", error="")
        else:
            try:
                self.db.execute("INSERT INTO publications(id,open_id,source,payload,hash,status) VALUES(?,?,?,?,?,'ready')", (identity, token["open_id"], data["source_video_id"], payload, digest))
                self.db.commit()
            except sqlite3.IntegrityError:
                self.db.rollback()
                raise Rejected("该账号已有同源视频的投稿记录，请核对原记录。")
        return self.receipt(self.publication(identity))

    def upload(self, identity, file, cover=False):
        p = self.publication(identity)
        if p["status"] not in ("ready", "failed"): raise Rejected("该记录不可重新上传素材。")
        token = self.token()
        if p["open_id"] != token["open_id"]: raise Rejected("账号不一致。")
        media_id = self.api.upload(token["access_token"], token["open_id"], file, cover)
        self.update(identity, **{"cover_id" if cover else "media_id": media_id}, status="ready", error="")
        return {"uploaded": True}

    def submit(self, identity):
        p = self.publication(identity)
        if p["status"] in ("submitted", "submission_unknown", "creating"): return self.receipt(p)
        token = self.token()
        if token["open_id"] != p["open_id"]: raise Rejected("账号不一致。")
        if not p["media_id"] or not p["cover_id"]: raise Rejected("请先上传视频和封面。")
        payload = json.loads(p["payload"])
        if payload.get("auto") and not self.auto: raise Rejected("自动发布权限已停用。")
        self.update(identity, status="creating", error="")  # durable intent BEFORE external POST
        try:
            result = self.api.call("/api/douyin/v1/video/create_video/", token=token["access_token"],
                query={"open_id": token["open_id"]}, data={"video_id": p["media_id"], "text": payload["text"],
                "custom_cover_image_url": p["cover_id"], "private_status": 0})
            remote = result.get("item_id") or result.get("video_id")
            if not remote: raise OSError("Missing receipt")
            self.update(identity, status="submitted", remote_id=str(remote))
        except Rejected as exc:
            definite = {"2100005", "28001003", "28001008", "28001014", "28001018", "28001019", "28001016", "28001007", "28003017", "10010", "10005", "10002"}
            # System/network errors inside the platform do not prove non-publication.
            self.update(identity, status="failed" if exc.code in definite else "submission_unknown", error=str(exc))
            if str(exc).startswith("RATE_LIMITED:"): self.save("rate_limited", "1")
            if str(exc).startswith("AUTH_REQUIRED:"): self.save("token", "")
        except Exception:
            self.update(identity, status="submission_unknown", error="创建请求结果不明；必须核对，禁止自动重发。")
        return self.receipt(self.publication(identity))

    def route(self, method, path, data):
        if path == "/v1/account" and method == "GET":
            try:
                token = self.token()
                account = {"client_key": self.key, "open_id": token["open_id"], "nickname": "抖音 · " + token["open_id"][-6:]}
            except Rejected: account = None
            return {"account": account, "capabilities": {"auto_publish": self.auto, "rate_limited": bool(self.value("rate_limited"))}}
        if path == "/v1/resume" and method == "POST":
            self.save("rate_limited", "")
            return {"resumed": True}
        if path == "/v1/auth/start" and method == "POST": return self.auth_start()
        if path in ("/v1/auth/clear", "/v1/auth/cancel", "/v1/auth/archive") and method == "POST":
            self.save("oauth", "")
            if path != "/v1/auth/cancel": self.save("token", "")
            if path == "/v1/auth/archive":
                if self.db.execute("SELECT 1 FROM publications WHERE status IN ('creating','submission_unknown')").fetchone(): raise Rejected("服务仍有结果待核对的记录。")
                self.save("identity", "")
            return {"cleared": True}
        match = re.fullmatch(r"/v1/publications/([a-fA-F0-9-]{36})(?:/(submit|resolve))?", path)
        if not match: raise Rejected("请求路径无效。")
        identity, action = match.groups()
        if method == "GET" and not action: return self.receipt(self.publication(identity))
        if method != "POST": raise Rejected("方法无效。")
        if action == "submit": return self.submit(identity)
        if action == "resolve":
            p = self.publication(identity)
            if p["status"] != "submission_unknown": return self.receipt(p)
            if data.get("not_submitted") is True: self.update(identity, status="ready", error="", media_id="", cover_id="")
            elif isinstance(data.get("remote_id"), str) and 1 <= len(data["remote_id"]) <= 512: self.update(identity, status="submitted", remote_id=data["remote_id"], error="人工核对")
            else: raise Rejected("作品 ID 无效。")
            return self.receipt(self.publication(identity))
        return self.register(identity, data)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # never log OAuth codes or credentials

    def handle_request(self):
        broker = self.server.broker
        self.connection.settimeout(120)
        parsed = urlsplit(self.path)
        try:
            if self.command == "GET" and parsed.path == "/oauth/callback":
                query = parse_qs(parsed.query)
                with broker.lock: result = broker.callback_code(query.get("code", [""])[0], query.get("state", [""])[0])
            else:
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + broker.pairing):
                    self.reply(401, {"error": "服务配对密钥无效。"})
                    return
                if self.headers.get("Transfer-Encoding"): raise Rejected("须提供 Content-Length。")
                length = int(self.headers.get("Content-Length", "0"))
                if self.command == "PUT":
                    match = re.fullmatch(r"/v1/publications/([a-fA-F0-9-]{36})/(video|cover)", parsed.path)
                    if not match: raise Rejected("上传路径无效。")
                    identity, kind = match.groups()
                    if not 0 < length <= (20 * 1024**2 if kind == "cover" else 4 * 1024**3): raise Rejected("素材大小不符合限制。")
                    with tempfile.NamedTemporaryFile(dir=broker.root, suffix=".upload", delete=False) as asset:
                        path = Path(asset.name)
                        try:
                            remaining = length
                            while remaining:
                                chunk = self.rfile.read(min(1024**2, remaining))
                                if not chunk: raise OSError("Incomplete upload")
                                asset.write(chunk)
                                remaining -= len(chunk)
                            asset.close()
                            with broker.lock: result = broker.upload(identity, path, kind == "cover")
                        finally:
                            asset.close()
                            path.unlink(missing_ok=True)
                else:
                    if not 0 <= length <= 65536: raise Rejected("请求过大。")
                    data = json.loads(self.rfile.read(length)) if length else {}
                    if not isinstance(data, dict): raise Rejected("请求须为对象。")
                    with broker.lock: result = broker.route(self.command, parsed.path, data)
            self.reply(200, result)
        except Rejected as exc:
            if str(exc).startswith("RATE_LIMITED:"):
                with broker.lock: broker.save("rate_limited", "1")
            if str(exc).startswith("AUTH_REQUIRED:"):
                with broker.lock: broker.save("token", "")
            self.reply(400, {"error": str(exc)})
        except ValueError: self.reply(400, {"error": "请求格式无效。"})
        except Exception: self.reply(503, {"error": "服务暂不可用；投稿结果不明时请查询原记录，勿重新创建。"})

    def reply(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = handle_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    from yt2bili.locking import FileLock
    with FileLock(Path(args.data_dir).resolve() / "broker.lock"):
        broker = Broker(args.data_dir, *(os.environ[name] for name in ("DOUYIN_CLIENT_KEY", "DOUYIN_CLIENT_SECRET",
            "DOUYIN_CALLBACK_URL", "DOUYIN_PAIRING_KEY", "DOUYIN_ENCRYPTION_KEY")), auto=os.environ.get("DOUYIN_APPROVED_AUTO_PUBLISH") == "1")
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        server.broker = broker
        try: server.serve_forever()
        finally:
            server.server_close()
            broker.db.close()


if __name__ == "__main__": main()
