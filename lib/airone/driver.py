"""Pairing for Navien AirOne units.

Uses Homey's built-in `login_credentials` → `list_devices` pair templates: the user
types their Navien account, we log in and enumerate the AirOne devices on the account,
and Homey renders the pickable list.

The account credentials are stored once at app scope (not per device), because every
device authenticates through the same cloud account and rotating the password should
repair them all at once. The per-device store keeps only the identifiers needed to
address that unit over REST and MQTT.
"""

from homey import driver

from lib import compat
from lib.const import (
    SETTING_HOME_SEQ,
    SETTING_PASSWORD,
    SETTING_USERNAME,
    STORE_DEVICE_ID,
    STORE_DEVICE_SEQ,
    STORE_MODEL_CODE,
    STORE_PHYSICAL_ID,
    STORE_SERVICE_CODE,
)
from lib.navien.airone import AironeDevice
from lib.navien.api import NavienApi, NavienAuthError


class AironeDriver(driver.Driver):

    async def on_init(self) -> None:
        self.log("Navien AirOne driver init")

    async def on_pair(self, session) -> None:
        # Held across the pair steps: a session is established once (by the saved
        # credentials if present, or the login form otherwise) and list_devices reuses it.
        state = {"api": None, "home_seq": None}

        async def _open_session(username: str, password: str):
            """Log in and stash the client + home on `state`. Raises on failure."""
            api = NavienApi(username=username, password=password, log=self.log)
            await api.login()
            homes = api.home_seqs()
            if not homes:
                raise Exception("계정에 등록된 집(home)이 없습니다.")
            saved = await compat.setting_get(self.homey, SETTING_HOME_SEQ)
            state["api"] = api
            state["home_seq"] = int(saved) if saved else homes[0][0]
            return homes

        async def on_check_session(data=None) -> dict:
            """Gate for the `start` view: try the saved account so device-add can skip
            the login form when the app is already signed in."""
            username = await compat.setting_get(self.homey, SETTING_USERNAME)
            password = await compat.setting_get(self.homey, SETTING_PASSWORD)
            if not username or not password:
                return {"ready": False, "reason": "먼저 나비엔 계정으로 로그인하세요."}
            try:
                await _open_session(username, password)
            except Exception as exc:
                return {"ready": False, "reason": f"저장된 계정으로 로그인하지 못했습니다: {exc}"}
            self.log("pair: reused saved session, skipping login")
            return {"ready": True}

        async def on_login(data) -> bool:
            username = (data or {}).get("username", "").strip()
            password = (data or {}).get("password", "")
            if not username or not password:
                raise Exception("아이디와 비밀번호를 입력하세요.")
            try:
                await _open_session(username, password)
            except NavienAuthError as exc:
                raise Exception(str(exc))
            # Persist app-scoped so devices — and the next pairing's start gate — can
            # re-authenticate without asking again.
            await compat.setting_set(self.homey, SETTING_USERNAME, username)
            await compat.setting_set(self.homey, SETTING_PASSWORD, password)
            await compat.setting_set(self.homey, SETTING_HOME_SEQ, str(state["home_seq"]))
            self.log(f"pair: login ok, home_seq={state['home_seq']}")
            return True

        async def on_list_devices(data=None) -> list:
            api = state["api"]
            if api is None:
                raise Exception("먼저 로그인하세요.")
            home_seq = state["home_seq"]
            raw_devices = await api.list_devices(home_seq)
            devices = []
            for raw in raw_devices:
                unit = AironeDevice.from_raw(raw, log=self.log)
                if unit is None:
                    continue
                devices.append({
                    "name": unit.nickname,
                    "data": {"id": str(unit.device_id)},
                    "store": {
                        STORE_DEVICE_SEQ: unit.device_seq,
                        STORE_DEVICE_ID: unit.device_id,
                        STORE_PHYSICAL_ID: unit.physical_device_id,
                        STORE_MODEL_CODE: unit.model_code,
                        STORE_SERVICE_CODE: 300,
                    },
                })
            self.log(f"pair: found {len(devices)} AirOne device(s)")
            return devices

        session.set_handler("check_session", on_check_session)
        session.set_handler("login", on_login)
        session.set_handler("list_devices", on_list_devices)
