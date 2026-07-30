"""Settings-page API.

Backs the app settings view where the user supplies (or updates) their Navien
account. Credentials are app-scoped rather than per-device: one account authenticates
to every appliance on it, so storing them once and rotating them once repairs all
paired devices together.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib import compat
from lib.const import (
    SETTING_HOME_SEQ,
    SETTING_PASSWORD,
    SETTING_UI_LANGUAGE,
    SETTING_USERNAME,
)
from lib.navien.api import NavienApi, NavienAuthError


def _body(kwargs: dict) -> dict:
    """Request body, however this Homey build delivers it (some pass `body`, some
    flatten into kwargs)."""
    body = kwargs.get("body")
    return body if isinstance(body, dict) else kwargs


def _log(homey, message: str) -> None:
    for target in (getattr(homey, "app", None), homey):
        log = getattr(target, "log", None)
        if callable(log):
            try:
                log(message)
                return
            except Exception:
                continue


async def get_status(homey, **kwargs) -> dict:
    """Whether an account is configured (never returns the password)."""
    username = await compat.setting_get(homey, SETTING_USERNAME)
    home_seq = await compat.setting_get(homey, SETTING_HOME_SEQ)
    return {
        "configured": bool(username),
        "username": username,
        "home_seq": home_seq,
    }


async def save_credentials(homey, **kwargs) -> dict:
    """Validate an account by logging in, then store it app-scoped."""
    body = _body(kwargs)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not username or not password:
        return {"ok": False, "error": "아이디와 비밀번호를 입력하세요."}

    api = NavienApi(username=username, password=password, log=lambda m: _log(homey, m))
    try:
        await api.login()
    except NavienAuthError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # network etc.
        return {"ok": False, "error": f"연결에 실패했습니다: {exc}"}

    homes = api.home_seqs()
    if not homes:
        return {"ok": False, "error": "계정에 등록된 집(home)이 없습니다."}

    await compat.setting_set(homey, SETTING_USERNAME, username)
    await compat.setting_set(homey, SETTING_PASSWORD, password)
    await compat.setting_set(homey, SETTING_HOME_SEQ, str(homes[0][0]))
    return {"ok": True, "homes": [{"seq": s, "name": n} for s, n in homes]}


async def clear_credentials(homey, **kwargs) -> dict:
    for key in (SETTING_USERNAME, SETTING_PASSWORD, SETTING_HOME_SEQ):
        await compat.setting_unset(homey, key)
    return {"ok": True}


async def set_language(homey, **kwargs) -> dict:
    """Let the settings webview report the UI language (Homey Python i18n can't)."""
    body = _body(kwargs)
    lang = str(body.get("language", "")).strip()
    if lang:
        await compat.remember_ui_language(homey, lang)
    return {"ok": True, "language": await compat.setting_get(homey, SETTING_UI_LANGUAGE)}
