"""Settings-page API.

Backs the app settings view where the user supplies (or updates) their Navien
account. Credentials are app-scoped rather than per-device: one account authenticates
to every appliance on it, so storing them once and rotating them once repairs all
paired devices together.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from navien_lib import compat
from navien_lib.const import (
    API_URL,
    IOT_ENDPOINT,
    SERVICE_AIRONE,
    SERVICE_MATE,
    SETTING_HOME_SEQ,
    SETTING_PASSWORD,
    SETTING_UI_LANGUAGE,
    SETTING_USERNAME,
)
from navien_lib.navien.api import NavienAuthError


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
    """Validate an account by logging in, then store it app-scoped.

    Validation goes through the app-wide shared session rather than a throwaway client of
    its own. Navien allows one session per account, so the throwaway used to log in a
    second time and bounce every running device with 404s the moment the user pressed save.
    """
    body = _body(kwargs)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not username or not password:
        return {"ok": False, "error": "아이디와 비밀번호를 입력하세요."}

    # Every failure below restores the shared session, because `reauth` points the shared
    # client at these credentials *before* it tries them (app.py's `_client` clears
    # access_token/aws in place). Without the restore, one typo would log every running
    # device out until the app was restarted: devices cache this object and never re-fetch
    # it, so the next poll would keep retrying the rejected password and the MQTT layer
    # would find no AWS credentials at all.
    try:
        api = await compat.reauth_shared_api(homey, username, password)
    except NavienAuthError as exc:
        await _restore_shared(homey)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # network etc.
        await _restore_shared(homey)
        return {"ok": False, "error": f"연결에 실패했습니다: {exc}"}

    homes = api.home_seqs()
    if not homes:
        # Also a failure path: the login succeeded, so the shared client is already holding
        # credentials this function is about to refuse to store.
        await _restore_shared(homey)
        return {"ok": False, "error": "계정에 등록된 집(home)이 없습니다."}

    await compat.setting_set(homey, SETTING_USERNAME, username)
    await compat.setting_set(homey, SETTING_PASSWORD, password)
    await compat.setting_set(homey, SETTING_HOME_SEQ, str(homes[0][0]))
    devices, _error = await _detect_devices(api, homes)
    return {
        "ok": True,
        "homes": [{"seq": s, "name": n} for s, n in homes],
        "devices": devices,
    }


async def _detect_devices(api, homes) -> tuple:
    """`(counts, error)` — the appliances visible on the account, by product, and whatever
    stopped us reading a home's list.

    The error is returned rather than swallowed because "연결 확인" has nothing else to
    check: `home_seqs()` reads a list already on the session, so a `_detect_devices` that
    reported nothing but zeroes made an unreachable server indistinguishable from an empty
    account. `save_credentials` still ignores it — by the time it calls this the login has
    succeeded and the settings are written, so a failed device count is a worse report than
    no report.
    """
    counts = {"airone": 0, "mate": 0, "other": 0, "total": 0}
    error = None
    for seq, _name in homes:
        try:
            raw_devices = await api.list_devices(seq)
        except Exception as exc:
            error = exc
            continue
        for raw in raw_devices or []:
            service = raw.get("serviceCode") or (raw.get("Properties") or {}).get("serviceCode")
            counts["total"] += 1
            if service == SERVICE_AIRONE:
                counts["airone"] += 1
            elif service == SERVICE_MATE:
                counts["mate"] += 1
            else:
                counts["other"] += 1
    return counts, error


async def _restore_shared(homey) -> None:
    """After a rejected save the shared client holds the rejected credentials; re-point it
    at whatever is still saved so the running devices keep working.

    The api.py counterpart of pairing._restore_shared. Unlike that one it reports what
    happened: a restore that fails leaves every device logged out, which is precisely the
    state worth a log line — pairing swallowing it silently is a separate bug.
    """
    try:
        await compat.shared_api(homey)
    except Exception as exc:
        _log(homey, f"navien: credentials restore FAILED after failed save: {exc}")
    else:
        _log(homey, "navien: credentials restored after failed save")


async def clear_credentials(homey, **kwargs) -> dict:
    # Disabled before the settings go, and the order is load-bearing. Each device caches
    # the session object it got from `_acquire_api` and never asks for another one, so
    # clearing the settings alone would leave every device polling a live session for an
    # account that no longer exists. Disabling first makes their next request raise before
    # it reaches the network — zero HTTP traffic while logged out.
    await compat.app_logout(homey)
    for key in (SETTING_USERNAME, SETTING_PASSWORD, SETTING_HOME_SEQ):
        await compat.setting_unset(homey, key)
    return {"ok": True}


def _mask(value: str) -> str:
    if not value:
        return ""
    return f"{value[0]}***{value[-1]}" if len(value) > 2 else "***"


async def diagnostics(homey, **kwargs) -> dict:
    """Non-sensitive status for the settings page (no password, masked account)."""
    username = await compat.setting_get(homey, SETTING_USERNAME)
    return {
        "configured": bool(username),
        "username_masked": _mask(username),
        "home_seq": await compat.setting_get(homey, SETTING_HOME_SEQ),
        "ui_language": await compat.setting_get(homey, SETTING_UI_LANGUAGE),
        "api_url": API_URL,
        "iot_endpoint": IOT_ENDPOINT,
    }


async def check_connection(homey, **kwargs) -> dict:
    """Report whether the saved account still works, on the session the devices are using.

    No restore is needed here, and none would help: this reads the *saved* credentials, so
    app.py's `_client` finds them identical to the ones the shared client already holds and
    leaves it untouched (its `!=` comparison is what clears the token). The throwaway
    client this used to build was a second session on an account that permits one, so
    pressing "연결 확인" logged out every running device.

    Sharing the session is also what stopped this from checking anything, which is why the
    two explicit verifications below exist. `shared_api` only logs in when `access_token` is
    falsy, so once any device had logged in it returned the live object untouched;
    `home_seqs()` is then a local read and `_detect_devices` swallowed its own failure, so
    "ok": True came back unconditionally and the `except NavienAuthError` branch was
    unreachable. It only did real work in the narrow window before the first device login —
    i.e. it was non-deterministically wrong.

      * `login_if_stale(api.auth_gen)` forces the two-step login the button is meant to be
        testing. It re-uses the one session rather than opening a second (that is the whole
        point of running it on the shared object), and handing it the *current* generation
        still lets it skip when a device logged in during the same instant.
      * the device list has to actually come back. That is the one call that proves the
        token the login just minted is accepted, and gating "ok" on it is what makes an
        unreachable server look different from an empty account.
    """
    username = await compat.setting_get(homey, SETTING_USERNAME)
    password = await compat.setting_get(homey, SETTING_PASSWORD)
    if not username or not password:
        return {"ok": False, "configured": False, "error": "저장된 계정이 없습니다."}
    try:
        api = await compat.shared_api(homey)
        await api.login_if_stale(api.auth_gen)
    except NavienAuthError as exc:
        return {"ok": False, "configured": True, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "configured": True, "error": f"연결에 실패했습니다: {exc}"}
    homes = api.home_seqs()
    devices, error = await _detect_devices(api, homes)
    if error is not None:
        return {"ok": False, "configured": True,
                "error": f"연결에 실패했습니다: {error}"}
    return {"ok": True, "configured": True,
            "homes": [{"seq": s, "name": n} for s, n in homes],
            "devices": devices}


async def set_language(homey, **kwargs) -> dict:
    """Let the settings webview report the UI language (Homey Python i18n can't)."""
    body = _body(kwargs)
    lang = str(body.get("language", "")).strip()
    if lang:
        await compat.remember_ui_language(homey, lang)
    return {"ok": True, "language": await compat.setting_get(homey, SETTING_UI_LANGUAGE)}
