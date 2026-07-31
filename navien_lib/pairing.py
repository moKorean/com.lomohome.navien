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


async def _login_handler(flow, data) -> bool:
    username = (data or {}).get("username", "").strip()
    password = (data or {}).get("password", "")
    if not username or not password:
        raise Exception("아이디와 비밀번호를 입력하세요.")
    try:
        await flow.open(username, password)
    except NavienAuthError as exc:
        raise Exception(str(exc)) from exc
    except TimeoutError:
        raise Exception(_SLOW_LOGIN) from None
    await flow.save(username, password)
    return True


def install(driver, session, build_devices) -> None:
    """Wire the standard pair handlers onto `session`."""
    flow = _Flow(driver)

    async def on_check_session(data=None) -> dict:
        username = await compat.setting_get(driver.homey, SETTING_USERNAME)
        password = await compat.setting_get(driver.homey, SETTING_PASSWORD)
        ready = bool(username and password)
        driver.log(f"pair: check_session ready={ready}")
        return {"ready": ready, "reason": "" if ready else "먼저 나비엔 계정으로 로그인하세요."}

    async def on_login(data) -> bool:
        return await _login_handler(flow, data)

    async def on_list_homes(data=None) -> list:
        await flow.ensure()
        return [{"seq": seq, "name": name} for seq, name in flow.homes]

    async def on_select_home(data=None) -> bool:
        seq = (data or {}).get("seq")
        if seq is None:
            return False
        flow.home_seq = int(seq)
        await compat.setting_set(driver.homey, SETTING_HOME_SEQ, str(flow.home_seq))
        driver.log(f"pair: home selected {flow.home_seq}")
        return True

    async def on_list_devices(data=None) -> list:
        try:
            await flow.ensure()
        except TimeoutError:
            raise Exception(_SLOW_LOGIN) from None
        devices = await asyncio.wait_for(
            build_devices(flow.api, flow.home_seq), timeout=LOGIN_TIMEOUT_S
        )
        driver.log(f"pair: found {len(devices)} device(s)")
        return devices

    session.set_handler("check_session", on_check_session)
    session.set_handler("login", on_login)
    session.set_handler("list_homes", on_list_homes)
    session.set_handler("select_home", on_select_home)
    session.set_handler("list_devices", on_list_devices)


def install_repair(driver, session) -> None:
    """Wire the repair handler: re-enter the account to refresh stored credentials."""
    flow = _Flow(driver)

    async def on_login(data) -> bool:
        return await _login_handler(flow, data)

    session.set_handler("login", on_login)
