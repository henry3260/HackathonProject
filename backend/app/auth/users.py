"""Email 帳號註冊／登入（Mock 模式）。

Milestone 1 以本機檔案儲存使用者與 opaque token，介面與正式版對齊：
- 密碼以 PBKDF2-HMAC-SHA256 + 隨機 salt 雜湊，永不明文儲存。
- 登入成功發一組隨機 token，get_current_user 以此換回 actorId（sub）。
- sub 一律由後端產生，前端不得自行指定使用者 ID（設計書 §9.1、§17.2）。
- 使用者與 token 落地在 `backend/.local-users.json`，因此 `uvicorn --reload`
  或重開後端後，已註冊帳號仍可登入、前端已保存的 token 也依然有效
  （示範帳號寫在 settings，本來就不受影響）。

正式版（Milestone 6）改用 Cognito：註冊／登入走 Cognito SignUp/InitiateAuth，
token 換成 Cognito JWT，此模組即可移除。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import uuid
from pathlib import Path

from .cognito import CurrentUser

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PBKDF2_ROUNDS = 200_000
_DEFAULT_STORAGE_PATH = Path(__file__).resolve().parents[2] / ".local-users.json"


class UserError(Exception):
    """帶錯誤碼的使用者操作例外，供 API 層轉成統一錯誤格式。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)


class UserStore:
    """使用者表（thread-safe，寫入本機檔案），模擬 Cognito User Pool。"""

    def __init__(self, storage_path: Path | None = _DEFAULT_STORAGE_PATH) -> None:
        self._by_email: dict[str, dict] = {}
        self._tokens: dict[str, str] = {}  # token -> sub
        self._by_sub: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._storage_path = storage_path
        self._load()

    # ---- 註冊 ----
    def register(self, email: str, password: str, name: str) -> tuple[CurrentUser, str]:
        email = (email or "").strip().lower()
        name = (name or "").strip()
        if not _EMAIL_RE.match(email):
            raise UserError("INVALID_EMAIL", "Email 格式不正確")
        if len(password or "") < 8:
            raise UserError("WEAK_PASSWORD", "密碼至少需 8 碼")
        if not name:
            name = email.split("@")[0]

        with self._lock:
            if email in self._by_email:
                raise UserError("EMAIL_EXISTS", "此 Email 已註冊")
            salt = os.urandom(16)
            sub = f"user-{uuid.uuid4().hex[:12]}"
            user = {
                "sub": sub,
                "email": email,
                "name": name,
                "salt": salt,
                "pw_hash": _hash_password(password, salt),
            }
            self._by_email[email] = user
            self._by_sub[sub] = user
            token = self._issue_token(sub)
            self._flush()
        return CurrentUser(sub=sub, name=name), token

    # ---- 登入 ----
    def login(self, email: str, password: str) -> tuple[CurrentUser, str]:
        email = (email or "").strip().lower()
        user = self._by_email.get(email)
        # 以固定 dummy 雜湊維持等時比較，避免以耗時差異列舉 Email。
        salt = user["salt"] if user else b"\x00" * 16
        expected = user["pw_hash"] if user else _hash_password("", salt)
        ok = hmac.compare_digest(_hash_password(password or "", salt), expected)
        if not user or not ok:
            raise UserError("INVALID_CREDENTIALS", "Email 或密碼錯誤")
        with self._lock:
            token = self._issue_token(user["sub"])
            self._flush()
        return CurrentUser(sub=user["sub"], name=user["name"]), token

    # ---- token 換使用者 ----
    def resolve(self, token: str) -> CurrentUser | None:
        sub = self._tokens.get(token)
        if not sub:
            return None
        user = self._by_sub.get(sub)
        if not user:
            return None
        return CurrentUser(sub=sub, name=user["name"], access_token=token)

    def logout(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)
            self._flush()

    def _issue_token(self, sub: str) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = sub
        return token

    # ---- 本機持久化 ----
    def _load(self) -> None:
        if not self._storage_path or not self._storage_path.exists():
            return
        try:
            payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except Exception:
            return

        for record in payload.get("users") or []:
            email = record.get("email")
            sub = record.get("sub")
            if not email or not sub:
                continue
            try:
                user = {
                    "sub": sub,
                    "email": email,
                    "name": record.get("name") or email.split("@")[0],
                    "salt": bytes.fromhex(record["salt"]),
                    "pw_hash": bytes.fromhex(record["pw_hash"]),
                }
            except (KeyError, ValueError):
                continue
            self._by_email[email] = user
            self._by_sub[sub] = user

        for token, sub in (payload.get("tokens") or {}).items():
            if sub in self._by_sub:
                self._tokens[token] = sub

    def _flush(self) -> None:
        """呼叫端必須已持有 self._lock。"""
        if not self._storage_path:
            return
        payload = {
            "users": [
                {
                    "sub": user["sub"],
                    "email": user["email"],
                    "name": user["name"],
                    "salt": user["salt"].hex(),
                    "pw_hash": user["pw_hash"].hex(),
                }
                for user in self._by_sub.values()
            ],
            "tokens": dict(self._tokens),
        }
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._storage_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, self._storage_path)


USERS = UserStore()
