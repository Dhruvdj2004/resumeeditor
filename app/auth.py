"""Single-user login: one email + password from the environment, remembered with a signed cookie."""
import hashlib
import hmac
import time

from .config import APP_EMAIL, APP_PASSWORD, LOGIN_DAYS

COOKIE = "resume_login"
MAX_FAILURES = 5  # per IP
MAX_TOTAL_FAILURES = 50  # across all IPs, since a client can fake its IP behind a proxy
LOCKOUT_SECONDS = 15 * 60

# Changing APP_EMAIL or APP_PASSWORD changes this key, which signs everyone out.
_KEY = hashlib.sha256(f"{APP_EMAIL}\0{APP_PASSWORD}".encode()).digest()
_failures: dict[str, list[float]] = {}


def enabled() -> bool:
    return bool(APP_PASSWORD)


def _sign(expires: int) -> str:
    return hmac.new(_KEY, f"login:{expires}".encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    expires = int(time.time()) + LOGIN_DAYS * 86400
    return f"{expires}.{_sign(expires)}"


def token_valid(token: str | None) -> bool:
    if not enabled():
        return True
    try:
        expires_s, sig = (token or "").split(".", 1)
        expires = int(expires_s)
    except ValueError:
        return False
    return expires > time.time() and hmac.compare_digest(sig, _sign(expires))


def locked_out(ip: str) -> bool:
    now = time.time()
    for key in list(_failures):
        _failures[key] = [t for t in _failures[key] if now - t < LOCKOUT_SECONDS]
        if not _failures[key]:
            del _failures[key]
    total = sum(len(v) for v in _failures.values())
    return len(_failures.get(ip, [])) >= MAX_FAILURES or total >= MAX_TOTAL_FAILURES


def check_credentials(ip: str, email: str, password: str) -> bool:
    ok = hmac.compare_digest(email.strip().lower().encode(), APP_EMAIL.encode()) & hmac.compare_digest(
        password.encode(), APP_PASSWORD.encode()
    )
    if ok:
        _failures.pop(ip, None)
    else:
        _failures.setdefault(ip, []).append(time.time())
    return ok
