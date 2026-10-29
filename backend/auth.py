"""Team logins + the agent key. Standard library only.

- People sign in with email + password and get a signed token (12h by default).
- Kora (the AI employee) uses the CRM_API_KEY header and acts with the 'agent' role.
- Every route declares which roles may call it.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict

from fastapi import Depends, HTTPException, Request

import db

ROLES = ("admin", "management", "marketing", "finance")
TOKEN_HOURS = float(os.environ.get("SESSION_HOURS", "12"))
API_KEY = os.environ.get("CRM_API_KEY")

_secret = None


def secret(c=None):
    """SECRET_KEY from env; otherwise generated once and kept in the database."""
    global _secret
    if _secret:
        return _secret
    env = os.environ.get("SECRET_KEY")
    if env:
        _secret = env.encode()
        return _secret
    with db.tx() as cx:
        s = db.get_setting(cx, "secret_key")
        if not s:
            s = secrets.token_urlsafe(48)
            db.set_setting(cx, "secret_key", s)
    _secret = s.encode()
    return _secret


# ------------------------------------------------------------ passwords
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 310_000)
    return "pbkdf2$310000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        new = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(new, base64.b64decode(dk))
    except Exception:
        return False


def validate_pw(pw: str):
    if len(pw or "") < 10:
        raise HTTPException(400, "Password must be at least 10 characters")


# ------------------------------------------------------------ tokens
def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(user):
    body = _b64(json.dumps({"uid": user["id"], "role": user["role"], "name": user["name"],
                            "exp": int(time.time() + TOKEN_HOURS * 3600)}).encode())
    sig = _b64(hmac.new(secret(), body.encode(), hashlib.sha256).digest())
    return body + "." + sig


def read_token(tok):
    try:
        body, sig = tok.split(".")
        good = _b64(hmac.new(secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(_unb64(body))
        if data["exp"] < time.time():
            return None
        return data
    except Exception:
        return None


# ------------------------------------------------------------ login throttle
_fails = defaultdict(list)
MAX_FAILS, WINDOW = 5, 15 * 60


def throttle_check(key):
    now = time.time()
    _fails[key] = [t for t in _fails[key] if now - t < WINDOW]
    if len(_fails[key]) >= MAX_FAILS:
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")


def throttle_fail(key):
    _fails[key].append(time.time())


def throttle_clear(key):
    _fails.pop(key, None)


# ------------------------------------------------------------ request identity
def current(request: Request):
    key = request.headers.get("x-api-key")
    if key and API_KEY and hmac.compare_digest(key, API_KEY):
        return {"id": 0, "name": "Kora", "role": "agent"}
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        data = read_token(auth[7:].strip())
        if data:
            with db.tx() as c:
                u = c.execute("SELECT id, name, role, active FROM users WHERE id=?", (data["uid"],)).fetchone()
            if u and u["active"]:
                return {"id": u["id"], "name": u["name"], "role": u["role"]}
    # local development only: no key, no users, not production
    if not API_KEY and os.environ.get("ENV") != "production":
        with db.tx() as c:
            if not c.execute("SELECT 1 FROM users").fetchone():
                return {"id": 0, "name": "dev", "role": "admin"}
    raise HTTPException(401, "Please sign in")


def require(*roles):
    """Dependency: allow these roles (admin is always allowed)."""
    allowed = set(roles) | {"admin"}

    def dep(user=Depends(current)):
        if "*" not in roles and user["role"] not in allowed:
            raise HTTPException(403, f"Your role ({user['role']}) can't do this")
        return user
    return dep


ANY = require("*")
