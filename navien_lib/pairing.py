"""Shared pairing + repair handlers for the Navien drivers.

Pairing reuses the app-wide shared session (one login per account — see app.py's
`shared_api`) and lists the devices of the driver's product; each driver differs only in
how it maps the raw list to Homey device payloads (`build_devices`). Login itself is not
part of pairing — the account is entered once in the app settings, so the start view just
checks whether one is saved and, if so, jumps to the device list.

Repair (after a password change) re-enters the account: it validates the new credentials
against the shared session, and only writes them to settings once they check out — so a
wrong entry never overwrites a working account. Because the shared client is updated in
place, running devices pick up the new credentials without a re-init.

Every network call is bounded by a hard timeout so a stalled request surfaces as a
visible error rather than an endless spinner.
"""

import asyncio

from navien_lib import compat
from navien_lib.const import SETTING_HOME_SEQ, SETTING_PASSWORD, SETTING_USERNAME
from navien_lib.navien.api import NavienAuthError

LOGIN_TIMEOUT_S = 25.0
_SLOW_LOGIN = "로그인 응답이 지연됩니다. 네트워크를 확인하고 다시 시도하세요."
_NEED_LOGIN = "먼저 앱 설정에서 나비엔 계정으로 로그인하세요."


def _payload(data, kwargs) -> dict:
    """The credentials dict, however this Homey build delivers it — positional, or wrapped
    in a `body`/`data` kwarg. Mirrors api.py's tolerance so the form never silently sees
    empty fields."""
    for candidate in (data, kwargs.get("body"), kwargs.get("data"), kwargs):
        if isinstance(candidate, dict) and ("username" in candidate or "password" in candidate):
            return candidate
    return data if isinstance(data, dict) else {}


async def _home_seq(homey, api) -> int:
    """The home to list devices for: the saved one, else the account's first."""
    saved = await compat.setting_get(homey, SETTING_HOME_SEQ)
    if saved:
        try:
            return int(saved)
        except (TypeError, ValueError):
            pass
    homes = api.home_seqs()
    return homes[0][0] if homes else 0


def install(driver, session, build_devices) -> None:
    """Wire the pair handlers onto `session` (check_session + list_devices)."""

    async def on_check_session(data=None, **kwargs) -> dict:
        username = await compat.setting_get(driver.homey, SETTING_USERNAME)
        password = await compat.setting_get(driver.homey, SETTING_PASSWORD)
        ready = bool(username and password)
        driver.log(f"pair: check_session ready={ready}")
        return {"ready": ready, "reason": "" if ready else _NEED_LOGIN}

    async def on_list_devices(data=None, **kwargs) -> list:
        try:
            api = await asyncio.wait_for(compat.shared_api(driver.homey), timeout=LOGIN_TIMEOUT_S)
        except TimeoutError:
            raise Exception(_SLOW_LOGIN) from None
        except Exception as exc:
            driver.log(f"pair: shared login failed: {exc}")
            raise Exception(_NEED_LOGIN) from exc
        home_seq = await _home_seq(driver.homey, api)
        devices = await asyncio.wait_for(
            build_devices(api, home_seq), timeout=LOGIN_TIMEOUT_S
        )
        driver.log(f"pair: found {len(devices)} device(s)")
        return devices

    session.set_handler("check_session", on_check_session)
    session.set_handler("list_devices", on_list_devices)


def install_repair(driver, session) -> None:
    """Wire the repair handler: validate new credentials, then store them."""

    async def on_login(data=None, **kwargs) -> bool:
        body = _payload(data, kwargs)
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        driver.log(f"repair: login attempt (user={'set' if username else 'empty'})")
        if not username or not password:
            raise Exception("아이디와 비밀번호를 입력하세요.")
        try:
            # Validate against the shared session (updated in place). Settings aren't
            # touched yet, so a wrong entry can't clobber the working account.
            await asyncio.wait_for(
                compat.reauth_shared_api(driver.homey, username, password),
                timeout=LOGIN_TIMEOUT_S,
            )
        except NavienAuthError as exc:
            await _restore_shared(driver.homey)
            raise Exception(str(exc)) from exc
        except TimeoutError:
            await _restore_shared(driver.homey)
            raise Exception(_SLOW_LOGIN) from None
        except Exception as exc:
            await _restore_shared(driver.homey)
            raise Exception(f"로그인에 실패했습니다: {exc}") from exc

        await compat.setting_set(driver.homey, SETTING_USERNAME, username)
        await compat.setting_set(driver.homey, SETTING_PASSWORD, password)
        api = await compat.shared_api(driver.homey)
        if not await compat.setting_get(driver.homey, SETTING_HOME_SEQ):
            home_seq = await _home_seq(driver.homey, api)
            if home_seq:
                await compat.setting_set(driver.homey, SETTING_HOME_SEQ, str(home_seq))
        driver.log("repair: credentials updated; shared session refreshed")
        return True

    session.set_handler("login", on_login)


async def _restore_shared(homey) -> None:
    """After a failed repair the shared client holds the rejected credentials; re-point it
    at whatever is still saved so the running devices keep working."""
    try:
        await compat.shared_api(homey)
    except Exception:
        pass
