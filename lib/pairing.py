"""Shared pairing handlers for the Navien drivers.

Both the AirOne and the Mat driver pair the same way: sign in to the Navien account
once (reusing saved credentials so device-add can skip the login form), then list the
devices of that product. Each driver only differs in how it turns the raw device list
into Homey device payloads, which it passes as `build_devices`.

Every network call is bounded by a hard timeout so a stalled request surfaces as a
visible error rather than an endless spinner.
"""

import asyncio

from lib import compat
from lib.const import SETTING_HOME_SEQ, SETTING_PASSWORD, SETTING_USERNAME
from lib.navien.api import NavienApi, NavienAuthError

LOGIN_TIMEOUT_S = 25.0
_SLOW_LOGIN = "로그인 응답이 지연됩니다. 네트워크를 확인하고 다시 시도하세요."


def install(driver, session, build_devices) -> None:
    """Wire the standard pair handlers onto `session`.

    `build_devices(api, home_seq)` is an async callable returning the list of Homey
    device payloads for this driver's product.
    """
    state = {"api": None, "home_seq": None}

    async def _open_session(username: str, password: str):
        api = NavienApi(username=username, password=password, log=driver.log)
        driver.log("pair: logging in to Navien cloud…")
        await asyncio.wait_for(api.login(), timeout=LOGIN_TIMEOUT_S)
        homes = api.home_seqs()
        if not homes:
            raise Exception("계정에 등록된 집(home)이 없습니다.")
        saved = await compat.setting_get(driver.homey, SETTING_HOME_SEQ)
        state["api"] = api
        state["home_seq"] = int(saved) if saved else homes[0][0]
        driver.log(f"pair: login ok, home_seq={state['home_seq']}")
        return homes

    async def _ensure_session():
        if state["api"] is not None:
            return
        username = await compat.setting_get(driver.homey, SETTING_USERNAME)
        password = await compat.setting_get(driver.homey, SETTING_PASSWORD)
        if not username or not password:
            raise Exception("먼저 나비엔 계정으로 로그인하세요.")
        await _open_session(username, password)

    async def on_check_session(data=None) -> dict:
        username = await compat.setting_get(driver.homey, SETTING_USERNAME)
        password = await compat.setting_get(driver.homey, SETTING_PASSWORD)
        ready = bool(username and password)
        driver.log(f"pair: check_session ready={ready}")
        return {"ready": ready, "reason": "" if ready else "먼저 나비엔 계정으로 로그인하세요."}

    async def on_login(data) -> bool:
        username = (data or {}).get("username", "").strip()
        password = (data or {}).get("password", "")
        if not username or not password:
            raise Exception("아이디와 비밀번호를 입력하세요.")
        try:
            await _open_session(username, password)
        except NavienAuthError as exc:
            raise Exception(str(exc)) from exc
        except TimeoutError:
            raise Exception(_SLOW_LOGIN) from None
        await compat.setting_set(driver.homey, SETTING_USERNAME, username)
        await compat.setting_set(driver.homey, SETTING_PASSWORD, password)
        await compat.setting_set(driver.homey, SETTING_HOME_SEQ, str(state["home_seq"]))
        return True

    async def on_list_devices(data=None) -> list:
        try:
            await _ensure_session()
        except TimeoutError:
            raise Exception(_SLOW_LOGIN) from None
        devices = await asyncio.wait_for(
            build_devices(state["api"], state["home_seq"]), timeout=LOGIN_TIMEOUT_S
        )
        driver.log(f"pair: found {len(devices)} device(s)")
        return devices

    session.set_handler("check_session", on_check_session)
    session.set_handler("login", on_login)
    session.set_handler("list_devices", on_list_devices)
