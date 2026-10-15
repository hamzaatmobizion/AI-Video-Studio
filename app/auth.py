"""Password hashing, session login and CSRF."""
import hmac
import secrets

import bcrypt
from fastapi import HTTPException, Request
from sqlalchemy import select

from app.models import User


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request) -> None:
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token", "")
    if not expected or not hmac.compare_digest(expected, sent):
        raise HTTPException(403, "Your session expired. Reload the page and try again.")


class LoginRequired(Exception):
    """Raised for HTML pages; turned into a redirect to /login."""


def current_user(request: Request) -> User | None:
    uid = request.session.get("uid")
    if uid is None:
        return None
    with request.app.state.db.session() as s:
        return s.scalar(select(User).where(User.id == uid))


def require_page_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def require_api_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Please sign in again.")
    return user


def require_post(request: Request) -> User:
    """Dependency for every state-changing route: signed in + valid CSRF header."""
    user = require_api_user(request)
    check_csrf(request)
    return user
