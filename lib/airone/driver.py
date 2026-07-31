"""Pairing for Navien AirOne units.

Signs into the Navien account (reusing saved credentials when present) and lists the
AirOne devices on it. The shared pairing flow lives in lib/pairing.py; this driver
only maps the raw device list to AirOne device payloads.
"""

from homey import driver

from lib import pairing
from lib.const import (
    STORE_DEVICE_ID,
    STORE_DEVICE_SEQ,
    STORE_MODEL_CODE,
    STORE_PHYSICAL_ID,
    STORE_SERVICE_CODE,
)
from lib.navien.airone import AironeDevice


class AironeDriver(driver.Driver):

    async def on_init(self) -> None:
        self.log("Navien AirOne driver init")

    async def on_pair(self, session) -> None:
        pairing.install(self, session, self._build_devices)

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
