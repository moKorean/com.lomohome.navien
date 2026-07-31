"""Pairing for Navien sleep mats.

Reuses the shared account login (lib/pairing.py) and lists the mats on the account.
Each mat's Homey capability set is computed from its model (single vs left/right zones,
temperature vs level type, four-season, power/safe flags) and shipped in the device
payload, so the device is created with exactly the capabilities that unit supports.
"""

from homey import driver

from lib import pairing
from lib.const import (
    STORE_DEVICE_ID,
    STORE_DEVICE_SEQ,
    STORE_MODEL_CODE,
    STORE_SERVICE_CODE,
)
from lib.navien.mate import MateDevice


class MateDriver(driver.Driver):

    async def on_init(self) -> None:
        self.log("Navien Mat driver init")

    async def on_pair(self, session) -> None:
        pairing.install(self, session, self._build_devices)

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
