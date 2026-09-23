"""GUI QR adapter for biliup's BiliTV login protocol.

Protocol reference: biliup/biliup crates/biliup/src/uploader/credential.rs.
The app identifier/signing constant below belongs to that public protocol,
not to the user's account. User tokens are never emitted to the frontend.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
import time
import uuid
from urllib.parse import urlencode

import requests
import qrcode

from yt2bili.desktop_settings import atomic_json
from yt2bili.identity import normalize_uid
from yt2bili.exceptions import Yt2BiliError


def validate_login(info):
    try:
        cookies = info["cookie_info"]["cookies"]
        names = {item["name"] for item in cookies if isinstance(item.get("value"), str)}
        token = info["token_info"]
        if not {"SESSDATA", "bili_jct", "DedeUserID"}.issubset(names):
            raise ValueError()
        if not isinstance(token["mid"], int) or not isinstance(token["access_token"], str):
            raise ValueError()
        if not isinstance(token["refresh_token"], str) or not isinstance(token["expires_in"], int):
            raise ValueError()
        if not isinstance(info["sso"], list):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Yt2BiliError("登录文件格式无效，请导入 biliup 生成的 Cookie JSON。") from exc
    return info


def signed_form(extra):
    fields = {"appkey": "4409e2ce8ffd12b8", "local_id": "0", "ts": int(time.time()), **extra}
    encoded = urlencode(sorted(fields.items()))
    fields["sign"] = hashlib.md5((encoded + "59b43e04ad6965f34319062b478f83dd").encode()).hexdigest()
    return fields


class LoginSession:
    def __init__(self, destination, emit, post=None, commit=None, account_id=None):
        self.destination, self.emit = destination, emit
        self.post = post or requests.post
        self.commit, self.account_id = commit, account_id
        self.cancelled = threading.Event()
        self.session_id = str(uuid.uuid4())
        self.thread = None
        self.guard = threading.Lock()

    def send(self, status, **extra):
        with self.guard:
            if not self.cancelled.is_set():
                self.emit("auth.status", {"session_id": self.session_id, "account_id": self.account_id, "status": status, **extra})

    def start(self):
        self.thread = threading.Thread(target=self._run, name="bili-login", daemon=True)
        self.thread.start()
        return {"session_id": self.session_id}

    def cancel(self):
        with self.guard:
            self.cancelled.set()

    def call(self, action, extra):
        result = self.post("https://passport.bilibili.com/x/passport-tv-login/qrcode/" + action,
                           data=signed_form(extra), timeout=20,
                           headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        result.raise_for_status()
        return result.json()

    def _run(self):
        try:
            self.send("loading")
            response = self.call("auth_code", {})
            if response.get("code") != 0:
                raise Yt2BiliError(f"二维码请求失败（代码 {response.get('code')}）。")
            data = response["data"]
            image = qrcode.make(data["url"])
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            self.send("waiting", qrcode="data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(), expires_in=180)
            deadline = time.monotonic() + 180
            while not self.cancelled.wait(2):
                if time.monotonic() >= deadline:
                    self.send("expired")
                    return
                result = self.call("poll", {"auth_code": data["auth_code"]})
                code = result.get("code")
                if code == 0:
                    info = validate_login({**result["data"], "platform": "BiliTV"})
                    with self.guard:
                        if self.cancelled.is_set():
                            return
                        if self.commit:
                            self.account_id = self.commit(info)["account_id"]
                        else:
                            atomic_json(self.destination, info)
                    self.send("success")
                    return
                if code in (86039, 86101):
                    continue
                if code == 86090:
                    self.send("scanned")
                    continue
                if code in (86038, 86009):
                    self.send("expired")
                    return
                raise Yt2BiliError(f"登录暂不可用（代码 {code}），请刷新二维码。")
        except Exception as exc:
            # Never include response bodies or token-bearing request URLs.
            self.send("failed", message=str(exc) if isinstance(exc, Yt2BiliError) else "登录网络请求失败，请检查网络后重试。")


def credential_identity(info):
    validate_login(info)
    uid = normalize_uid(info["token_info"]["mid"])
    cookies = {item["name"]: item["value"] for item in info["cookie_info"]["cookies"]}
    if uid != normalize_uid(cookies["DedeUserID"]):
        raise Yt2BiliError("Cookie 与 Token 的账号 UID 不一致，未替换原登录态。")
    return uid


def verify_credentials(info):
    uid = credential_identity(info)
    cookies = {item["name"]: item["value"] for item in info["cookie_info"]["cookies"]}
    try:
        response = requests.get("https://api.bilibili.com/x/web-interface/nav", cookies=cookies, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise Yt2BiliError("账号验证暂不可用，请检查网络后重试。") from exc
    if data.get("code") != 0 or not data.get("data", {}).get("isLogin"):
        raise Yt2BiliError("登录失效，请重新登录该账号。")
    if normalize_uid(data["data"].get("mid")) != uid:
        raise Yt2BiliError("在线账号与目标 UID 不一致，已阻止投稿。")
    return {"uid": uid, "nickname": data["data"].get("uname", "")}


def account_status(path, verify=False):
    if not path.is_file():
        return {"configured": False, "verified": False}
    info = validate_login(json.loads(path.read_text(encoding="utf-8")))
    status = {"configured": True, "verified": False, "mid": str(info["token_info"]["mid"])}
    if verify:
        cookies = {item["name"]: item["value"] for item in info["cookie_info"]["cookies"]}
        response = requests.get("https://api.bilibili.com/x/web-interface/nav", cookies=cookies, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        data = response.json()
        status["verified"] = data.get("code") == 0 and bool(data.get("data", {}).get("isLogin"))
        status["name"] = data.get("data", {}).get("uname", "")
    return status
