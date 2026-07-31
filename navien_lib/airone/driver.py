"""Pairing for Navien AirOne units.

Signs into the Navien account (reusing saved credentials when present) and lists the
AirOne devices on it. The shared pairing flow lives in navien_lib/pairing.py; this driver
only maps the raw device list to AirOne device payloads.
"""

from homey import driver

from navien_lib import compat, pairing
from navien_lib.const import (
    STORE_DEVICE_ID,
    STORE_DEVICE_SEQ,
    STORE_MODEL_CODE,
    STORE_PHYSICAL_ID,
    STORE_SERVICE_CODE,
)
from navien_lib.navien.airone import AironeDevice


class AironeDriver(driver.Driver):

    async def on_init(self) -> None:
        self.log("Navien AirOne driver init")
        self._register_flow_cards()

    def _register_flow_cards(self) -> None:
        """Wire the Flow condition/action cards to the device methods.

        Cards are app-global; each carries a `device` arg (filtered to this driver) that
        resolves to the AironeDevice instance the Flow targets. Failures here must not
        abort driver init, so the whole block is guarded.
        """
        # Run listeners take (args, state) but Homey also passes extra keywords such as
        # `manual`, so every handler must accept **kwargs or the card errors out.
        try:
            self._bind("condition", "airone_mode_is",
                       lambda a, s=None, **_: a["device"].flow_mode_id() == a["mode"])
            self._bind("condition", "airone_fan_is",
                       lambda a, s=None, **_: a["device"].flow_fan_id() == a["fan"])
            self._bind("action", "airone_set_mode",
                       lambda a, s=None, **_: a["device"].flow_set_mode(a["mode"]))
            self._bind("action", "airone_set_fan",
                       lambda a, s=None, **_: a["device"].flow_set_fan(a["fan"]))
            self._bind("action", "airone_set_power",
                       lambda a, s=None, **_: a["device"].flow_set_power(a["power"] == "on"))
            self._bind("action", "airone_set_humidity",
                       lambda a, s=None, **_: a["device"].flow_set_humidity(int(a["humidity"])))
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
        return devices
