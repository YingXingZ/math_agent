"""Small, dependency-free account and tenancy layer for the teacher workbench.

Opaque session cookies avoid putting roles or personal data in browser-visible
tokens.  Passwords use PBKDF2-SHA256; this is intentionally not a home-grown
encryption scheme and can later be migrated to a dedicated identity provider.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import secrets
from typing import Iterable

from fastapi import HTTPException, Request

from .config import settings
from .db import connection


DEV_ACTOR = {"id": None, "username": "local-admin", "display_name": "本机开发管理员", "role": "admin", "dev_mode": True}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str, salt: bytes | None = None) -> str:
    if not password or len(password) < 10:
        raise HTTPException(422, "密码至少 10 位")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_bootstrap_admin() -> None:
    """Create the first admin only from explicitly supplied environment secrets."""
    if not settings.bootstrap_admin_username or not settings.bootstrap_admin_password:
        return
    with connection() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO users(username,display_name,password_hash,role) VALUES(?,?,?,'admin')",
                (settings.bootstrap_admin_username.strip(), "系统管理员", hash_password(settings.bootstrap_admin_password)),
            )


def login(username: str, password: str) -> tuple[dict, str, datetime]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (username.strip(),)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            raise HTTPException(401, "账号或密码错误")
        token = secrets.token_urlsafe(32)
        expires_at = _utc_now() + timedelta(days=settings.session_days)
        conn.execute(
            "INSERT INTO user_sessions(token_hash,user_id,expires_at,last_seen_at) VALUES(?,?,?,?)",
            (token_hash(token), row["id"], expires_at.isoformat(), _utc_now().isoformat()),
        )
    return _public_user(dict(row)), token, expires_at


def current_user(request: Request) -> dict:
    if not settings.auth_required:
        return DEV_ACTOR.copy()
    cached = getattr(request.state, "user", None)
    if cached is not None:
        return cached
    token = request.cookies.get(settings.session_cookie_name, "")
    if not token:
        raise HTTPException(401, "请先登录")
    with connection() as conn:
        row = conn.execute(
            """SELECT u.id,u.username,u.display_name,u.role,u.active,s.id AS session_id,s.expires_at
               FROM user_sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.revoked_at IS NULL""",
            (token_hash(token),),
        ).fetchone()
        if not row or not row["active"] or datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc) <= _utc_now():
            raise HTTPException(401, "登录已过期，请重新登录")
        conn.execute("UPDATE user_sessions SET last_seen_at=? WHERE id=?", (_utc_now().isoformat(), row["session_id"]))
    user = _public_user(dict(row))
    request.state.user = user
    return user


def require_roles(request: Request, roles: Iterable[str]) -> dict:
    user = current_user(request)
    if user["role"] not in set(roles):
        raise HTTPException(403, "当前账号无此权限")
    return user


def teacher_id_for_scope(user: dict) -> int | None:
    """Admin sees all; teacher sees own tenant; dev mode intentionally unscoped."""
    return None if user["role"] == "admin" or user.get("dev_mode") else int(user["id"])


def audit(actor: dict | None, action: str, resource_type: str, resource_id: object | None = None,
          tenant_teacher_id: int | None = None, metadata: dict | None = None, ip: str | None = None) -> None:
    import json
    with connection() as conn:
        conn.execute(
            """INSERT INTO audit_logs(actor_user_id,tenant_teacher_id,action,resource_type,resource_id,metadata_json,ip)
               VALUES(?,?,?,?,?,?,?)""",
            (actor.get("id") if actor else None, tenant_teacher_id, action, resource_type,
             str(resource_id) if resource_id is not None else None, json.dumps(metadata or {}, ensure_ascii=False), ip),
        )


def _public_user(row: dict) -> dict:
    return {key: row[key] for key in ("id", "username", "display_name", "role")}
