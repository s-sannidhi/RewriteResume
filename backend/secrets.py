"""Secrets live in the OS credential store (or env vars), never in profile.json.

Two secrets, one service name each:
  - resume-rewriter-login   account=<login email>   env RR_LOGIN_PASSWORD
  - resume-rewriter-gmail   account=<gmail address> env RR_GMAIL_APP_PASSWORD
(Ask runs on the local Ollama model — no key stored here.)

Backends, tried in order, so the same code works on every OS:
  1. env var                       — always wins; how CI/servers inject a secret
  2. `keyring` package, if present — Windows Credential Manager, macOS Keychain, Linux Secret
                                     Service. This is what makes Windows work; it is an OPTIONAL
                                     dependency, so a machine without it still runs fine.
  3. macOS `security` binary       — the original path, kept so existing Keychain entries on this
                                     machine keep working even without `keyring` installed.
Results are cached in-process; call invalidate() after a set_secret from the same process.
"""
import os
import subprocess
import sys

SERVICE_LOGIN = "resume-rewriter-login"
SERVICE_GMAIL = "resume-rewriter-gmail"

_cache: dict[tuple[str, str], str | None] = {}


def _keyring():
    """The keyring module if it is installed AND has a working backend, else None."""
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None                     # installed but no usable OS store (headless Linux)
        return keyring
    except Exception:
        return None


def _mac_security_get(service: str, account: str) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10)
        return r.stdout.rstrip("\n") if r.returncode == 0 and r.stdout else None
    except Exception:
        return None


def _mac_security_set(service: str, account: str, value: str) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", value],
            capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def backend_name() -> str:
    """Which store this machine will use — surfaced by setup so the user knows where a password
    is going before they type one."""
    if _keyring():
        return "keyring (OS credential store)"
    if sys.platform == "darwin":
        return "macOS Keychain (security)"
    return "environment variables only"


def get_secret(service: str, account: str, env_var: str | None = None) -> str | None:
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    key = (service, account)
    if key in _cache:
        return _cache[key]
    val = None
    kr = _keyring()
    if kr:
        try:
            val = kr.get_password(service, account)
        except Exception:
            val = None
    if val is None:
        val = _mac_security_get(service, account)
    _cache[key] = val
    return val


def set_secret(service: str, account: str, value: str) -> bool:
    ok = False
    kr = _keyring()
    if kr:
        try:
            kr.set_password(service, account, value)
            ok = True
        except Exception:
            ok = False
    if not ok:
        ok = _mac_security_set(service, account, value)
    if ok:
        _cache[(service, account)] = value
    return ok


def invalidate(service: str | None = None, account: str | None = None) -> None:
    if service is None:
        _cache.clear()
    else:
        _cache.pop((service, account or ""), None)


def login_account(profile: dict) -> str:
    login = profile.get("login_credentials") or {}
    ident = profile.get("identity") or {}
    return login.get("email") or ident.get("email") or "default"


def login_password(profile: dict) -> str | None:
    return get_secret(SERVICE_LOGIN, login_account(profile), "RR_LOGIN_PASSWORD")


def scrub_profile(profile: dict) -> tuple[dict, bool]:
    """If the profile carries a plaintext login password, move it to the Keychain and strip it.
    Returns (profile, changed)."""
    login = profile.get("login_credentials") or {}
    pw = (login.get("password") or "").strip()
    if not pw:
        return profile, False
    set_secret(SERVICE_LOGIN, login_account(profile), pw)
    login["password"] = ""
    profile["login_credentials"] = login
    return profile, True
