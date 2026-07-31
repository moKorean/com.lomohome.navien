"""Pairing for standalone Navien AirMonitor units.

An AirMonitor is separate hardware attached to an AirOne; the upstream integration
exposes it as its own device. Here it becomes its own Homey device whose air-quality
sensors are read from the parent AirOne's `/air-sensor` REST endpoint, filtered to the
monitor's sensor zone.
"""

from homey import driver

from lib import pairing
from lib.const import STORE_DEVICE_SEQ, STORE_MONITOR_ID, STORE_ZONE_ID
from lib.navien.airone import AironeDevice


class AirMonitorDriver(driver.Driver):

    async def on_init(self) -> None:
        self.log("Navien AirMonitor driver init")

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
            for mon in unit.air_monitors():
                devices.append({
                    "name": f"{unit.nickname} 에어모니터",
                    "data": {"id": mon["monitor_id"]},
                    "store": {
                        STORE_DEVICE_SEQ: unit.device_seq,   # parent, for /air-sensor
                        STORE_MONITOR_ID: mon["monitor_id"],
                        STORE_ZONE_ID: mon["zone_id"],
                    },
                })
        return devices
