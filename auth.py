import hashlib
import secrets

import bcrypt
from fastapi import HTTPException, Request

import social_db as sdb


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def generate_reset_token() -> tuple[str, str]:
    """Returns (raw_token, token_hash). Only the hash is stored in the DB --
    the raw token goes in the emailed link and is never persisted, so a DB
    leak alone can't be used to reset anyone's password."""
    raw_token = secrets.token_urlsafe(32)
    return raw_token, hash_token(raw_token)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def log_in_user(request: Request, user_id: int) -> None:
    request.session["user_id"] = user_id


def log_out_user(request: Request) -> None:
    request.session.pop("user_id", None)


def get_current_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    return sdb.get_user_by_id(user_id)


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def require_room_access(request: Request, room_id: int) -> dict:
    user = require_user(request)
    if not sdb.user_can_access_room(user["id"], room_id):
        raise HTTPException(status_code=403, detail="Not a member of this chat")
    return user
