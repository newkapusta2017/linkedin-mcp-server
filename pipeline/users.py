# pipeline/users.py
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR = Path.home() / ".linkedin-mcp"
USERS_FILE = BASE_DIR / "users.json"


def _load_users() -> list[dict[str, Any]]:
    if not USERS_FILE.exists():
        return []
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        return data.get("users", [])
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", USERS_FILE, exc)
        return []


def _save_users(users: list[dict[str, Any]]) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"users": users}, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=BASE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, USERS_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def user_dir(user_id: str) -> Path:
    return BASE_DIR / f"user_{user_id}"


def get_user(user_id: str) -> dict[str, Any] | None:
    for u in _load_users():
        if u["id"] == user_id:
            return u
    return None


def list_active_users() -> list[dict[str, Any]]:
    return [u for u in _load_users() if u.get("status") == "active"]


def create_user(user_id: str, name: str, telegram_chat_id: str) -> dict[str, Any]:
    users = _load_users()
    if any(u["id"] == user_id for u in users):
        raise ValueError(f"User {user_id} already exists")

    from datetime import date
    user = {
        "id": user_id,
        "name": name,
        "telegram_chat_id": telegram_chat_id,
        "status": "pending_linkedin",
        "created_at": date.today().isoformat(),
    }
    d = user_dir(user_id)
    d.mkdir(parents=True, exist_ok=True)
    users.append(user)
    _save_users(users)
    logger.info("Created user %s (dir=%s)", user_id, d)
    return user


def update_status(user_id: str, status: str) -> None:
    users = _load_users()
    for u in users:
        if u["id"] == user_id:
            u["status"] = status
            break
    _save_users(users)
