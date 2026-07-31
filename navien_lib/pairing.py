"""Shared pairing + repair handlers for the Navien drivers.

Both drivers pair the same way: sign in to the Navien account once (reusing saved
credentials so device-add can skip the login form), optionally pick a home when the
account has several, then list the devices of that product. Each driver only differs
in how it turns the raw device list into Homey device payloads (`build_devices`).

Repair reuses the same login to re-store credentials after a password change.

Every network call is bounded by a hard timeout so a stalled request surfaces as a
visible error rather than an endless spinner.
"""

import asyncio

from navien_lib import compat
from navien_lib.const import SETTING_HOME_SEQ, SETTING_PASSWORD, SETTING_USERNAME
from navien_lib.navien.api import NavienApi, NavienAuthError

LOGIN_TIMEOUT_S = 25.0
_SLOW_LOGIN = "로그인 응답이 지연됩니다. 네트워크를 확인하고 다시 시도하세요."


class _Flow:
    """Holds the authenticated client across the steps of one pair/repair session."""

    def __init__(self, driver):
        self.driver = driver
        self.api = None
        self.home_seq = None
        self.homes = []

    async def open(self, username: str, password: str):
        api = NavienApi(username=username, password=password, log=self.driver.log)
        self.driver.log("pair: logging in to Navien cloud…")
        await asyncio.wait_for(api.login(), timeout=LOGIN_TIMEOUT_S)
        homes = api.home_seqs()
        if not homes:
            raise Exception("계정에 등록된 집(home)이 없습니다.")
        saved = await compat.setting_get(self.driver.homey, SETTING_HOME_SEQ)
        self.api = api
        self.homes = homes
        self.home_seq = int(saved) if saved else homes[0][0]
        self.driver.log(f"pair: login ok, home_seq={self.home_seq}")
        return homes

    async def ensure(self):
        if self.api is not None:
            return
        username = await compat.setting_get(self.driver.homey, SETTING_USERNAME)
        password = await compat.setting_get(self.driver.homey, SETTING_PASSWORD)
        if not username or not password:
            raise Exception("먼저 나비엔 계정으로 로그인하세요.")
        await self.open(username, password)

    async def save(self, username: str, password: str):
        await compat.setting_set(self.driver.homey, SETTING_USERNAME, username)
        await compat.setting_set(self.driver.homey, SETTING_PASSWORD, password)
        await compat.setting_set(self.driver.homey, SETTING_HOME_SEQ, str(self.home_seq))


def _payload(data, kwargs) -> dict:
    """The credentials dict, however this Homey build delivers it. The login_credentials
    template usually passes it as the first positional arg, but some builds wrap it in a
    `body`/`data` kwarg — mirror api.py's tolerance so device-login never silently sees
    empty fields."""
    for candidate in (data, kwargs.get("body"), kwargs.get("data"), kwargs):
        if isinstance(candidate, dict) and ("username" in candidate or "password" in candidate):
            return candidate
    return data if isinstance(data, dict) else {}


async def _login_handler(flow, data, kwargs) -> bool:
    body = _payload(data, kwargs)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    flow.driver.log(f"pair: login attempt (user={'set' if username else 'empty'})")
    if not username or not password:
        raise Exception("아이디와 비밀번호를 입력하세요.")
    try:
        await flow.open(username, password)
    except NavienAuthError as exc:
        flow.driver.log(f"pair: login rejected: {exc}")
        raise Exception(str(exc)) from exc
    except TimeoutError:
        raise Exception(_SLOW_LOGIN) from None
    await flow.save(username, password)
    flow.driver.log(f"pair: login ok, credentials saved (home_seq={flow.home_seq})")
    return True


_NEED_LOGIN = "먼저 앱 설정에서 나비엔 계정으로 로그인하세요."


def install(driver, session, build_devices) -> None:
    """Wire the pair handlers onto `session`.

    Login is intentionally *not* part of pairing — the built-in login_credentials
    template behaved inconsistently (accepting invalid input and advancing anyway), so
    the account is entered once in the app settings and pairing only reuses it. The
    start view checks `check_session` and, when an account is saved, jumps to the device
    list; otherwise it tells the user to sign in from the app settings.
    """
    flow = _Flow(driver)

    async def on_check_session(data=None, **kwargs) -> dict:
        username = await compat.setting_get(driver.homey, SETTING_USERNAME)
        password = await compat.setting_get(driver.homey, SETTING_PASSWORD)
        ready = bool(username and password)
        driver.log(f"pair: check_session ready={ready}")
        return {"ready": ready, "reason": "" if ready else _NEED_LOGIN}

    async def on_list_devices(data=None, **kwargs) -> list:
        try:
            await flow.ensure()
        except TimeoutError:
            raise Exception(_SLOW_LOGIN) from None
        except Exception as exc:
            driver.log(f"pair: list_devices ensure failed: {exc}")
            raise Exception(_NEED_LOGIN) from exc
        devices = await asyncio.wait_for(
            build_devices(flow.api, flow.home_seq), timeout=LOGIN_TIMEOUT_S
        )
        driver.log(f"pair: found {len(devices)} device(s)")
        return devices

    session.set_handler("check_session", on_check_session)
    session.set_handler("list_devices", on_list_devices)


def install_repair(driver, session) -> None:
    """Wire the repair handler: re-enter the account to refresh stored credentials."""
    flow = _Flow(driver)

    async def on_login(data=None, **kwargs) -> bool:
        return await _login_handler(flow, data, kwargs)

    session.set_handler("login", on_login)
