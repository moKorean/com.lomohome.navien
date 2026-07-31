"""One paired Navien AirMonitor.

Reads air quality from the parent AirOne's `/air-sensor` REST endpoint (air quality is
REST-only — MQTT carries the sensor kinds but not their values) and applies the reading
for this monitor's zone to standard/custom measure capabilities. Read-only, no MQTT.
"""

import asyncio

from homey import device

from navien_lib import compat
from navien_lib.const import (
    POLL_INTERVAL_S,
    SETTING_HOME_SEQ,
    STORE_DEVICE_SEQ,
    STORE_MONITOR_ID,
    STORE_ZONE_ID,
)
from navien_lib.navien.airone import parse_air_sensors_for

# AirMonitor uses the standard measure_pm10 for 미세먼지 (AirOne uses navien_pm10); map
# both so whichever capability the driver actually has gets populated. _set() skips any
# capability the device doesn't expose.
_SENSOR_KINDS = {
    "measure_temperature": "temperature",
    "measure_humidity": "humidity",
    "measure_pm25": "pm25",
    "measure_pm10": "pm10",
    "measure_co2": "co2",
    "navien_pm1": "pm1",
    "navien_pm10": "pm10",
    "navien_tvoc": "tvoc",
    "navien_radon": "radon",
}

_GRADE_KINDS = {
    "navien_tvoc_grade": "tvoc",
    "navien_radon_grade": "radon",
}


class AirMonitorDevice_(device.Device):

    async def on_init(self) -> None:
        store = self.get_store()
        self._parent_seq = store.get(STORE_DEVICE_SEQ)
        self._monitor_id = str(store.get(STORE_MONITOR_ID) or "")
        self._zone_id = store.get(STORE_ZONE_ID)
        self._home_seq = int(await compat.setting_get(self.homey, SETTING_HOME_SEQ) or 0)
        # The Navien session is shared app-wide (one session per account); acquired in
        # _run.
        self._api = None
        self._sensors: dict = {}
        self._poll_task = asyncio.create_task(self._run())

    async def on_uninit(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()

    async def _acquire_api(self) -> None:
        delay = 5
        while True:
            try:
                self._api = await compat.shared_api(self.homey)
                return
            except Exception as exc:
                self.log(f"login pending ({exc}); retrying in {delay}s")
                await self._safe_unavailable("나비엔 서버 로그인 재시도 중…")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)

    async def _run(self) -> None:
        await self._acquire_api()
        await self._poll_once()
        await self._safe_available()
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"poll failed: {exc}")

    async def _poll_once(self) -> None:
        sensors = await self._api.air_sensor(self._parent_seq, self._home_seq)
        parsed = parse_air_sensors_for(sensors, self._zone_id, self._monitor_id)
        if parsed:  # merge — an empty poll must not wipe the values
            self._sensors.update(parsed)
        await self._apply()

    async def _apply(self) -> None:
        for capability, kind in _SENSOR_KINDS.items():
            reading = self._sensors.get(kind) or {}
            await self._set(capability, self._num(reading.get("value")))
        for capability, kind in _GRADE_KINDS.items():
            reading = self._sensors.get(kind) or {}
            await self._set(capability, reading.get("level") or None)
        total = self._sensors.get("total") or {}
        await self._set("navien_air_grade", self._num(total.get("value")))

    async def _set(self, capability: str, value) -> None:
        if value is None or capability not in self.get_capabilities():
            return
        try:
            if self.get_capability_value(capability) != value:
                await self.set_capability_value(capability, value)
        except Exception as exc:
            self.log(f"set {capability} failed: {exc}")

    @staticmethod
    def _num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def _safe_available(self):
        try:
            await self.set_available()
        except Exception:
            pass

    async def _safe_unavailable(self, reason):
        try:
            await self.set_unavailable(reason)
        except Exception:
            pass
