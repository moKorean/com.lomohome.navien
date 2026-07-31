"""Pairing for Navien sleep mats.

Reuses the shared account login (navien_lib/pairing.py) and lists the mats on the account.
Each mat's Homey capability set is computed from its model (single vs left/right zones,
temperature vs level type, four-season, power/safe flags) and shipped in the device
payload, so the device is created with exactly the capabilities that unit supports.
"""

from homey import driver

from navien_lib import compat, pairing
from navien_lib.const import (
    STORE_DEVICE_ID,
    STORE_DEVICE_SEQ,
    STORE_MODEL_CODE,
    STORE_SERVICE_CODE,
)
from navien_lib.navien.mate import MateDevice


class MateDriver(driver.Driver):

    async def on_init(self) -> None:
        self.log("Navien Mat driver init")
        self._register_flow_cards()

    def _register_flow_cards(self) -> None:
        """Wire the sleep-mat Flow condition/action cards to the device methods.

        Run listeners take (args, state) plus extra keywords (e.g. `manual`), so each
        handler accepts **kwargs. Failures here must not abort driver init.
        """
        try:
            self._bind("condition", "mate_power_is",
                       lambda a, s=None, **_: a["device"].flow_is_on())
            self._bind("condition", "mate_season_is",
                       lambda a, s=None, **_: a["device"].flow_season() == int(a["season"]))
            self._bind("action", "mate_set_power",
                       lambda a, s=None, **_: a["device"].flow_set_power(a["power"] == "on"))
            self._bind("action", "mate_set_season",
                       lambda a, s=None, **_: a["device"].flow_set_season(int(a["season"])))
            self._bind("action", "mate_set_temperature",
                       lambda a, s=None, **_: a["device"].flow_set_temperature(
                           a["zone"], float(a["temperature"])))
            self._bind("action", "mate_set_level",
                       lambda a, s=None, **_: a["device"].flow_set_level(
                           a["zone"], int(a["level"])))
        except Exception as exc:
            self.log(f"flow card registration failed: {exc}")

    def _bind(self, kind: str, card_id: str, handler) -> None:
        card = compat.flow_card(self.homey, kind, card_id)
        compat.register_run_listener(card, handler)

    async def on_pair(self, session) -> None:
        pairing.install(self, session, self._build_devices)

    async def on_repair(self, session, device=None) -> None:
        pairing.install_repair(self, session)

    async def _build_devices(self, api, home_seq) -> list:
        devices = []
        for raw in await api.list_devices(home_seq):
            mat = MateDevice.from_raw(raw, log=self.log)
            if mat is None:
                continue
            devices.append({
                "name": mat.nickname,
                "data": {"id": str(mat.device_id)},
                "store": {
                    STORE_DEVICE_SEQ: mat.device_seq,
                    STORE_DEVICE_ID: mat.device_id,
                    STORE_MODEL_CODE: mat.model_code,
                    STORE_SERVICE_CODE: 200,
                },
                "capabilities": mat.homey_capabilities(),
                "capabilitiesOptions": mat.homey_capability_options(),
            })
        return devices
