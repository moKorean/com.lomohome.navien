"""One paired Navien AirOne unit.

Logs into the cloud, subscribes to the realtime MQTT push for fresh state, and falls
back to a slow REST re-read (which also carries the air-quality readings MQTT does not).
Control is REST: a capability change posts a command and the device's own reply, pushed
back over MQTT, is what updates the capability — so the UI reflects the appliance, not
an optimistic guess. Air-quality is REST-only.

State is deep-merged, never replaced, because reports arrive partial. See docs/PORTING.md.
"""

import asyncio

from homey import device

from lib import compat
from lib.const import (
    AIRONE_CMD_CHANGE_MODE,
    AIRONE_CMD_POWER,
    AIRONE_CMD_STATUS,
    AIRONE_READBACK_DELAY_S,
    HUMIDITY_STEP,
    POLL_INTERVAL_S,
    SETTING_HOME_SEQ,
    SETTING_PASSWORD,
    SETTING_USERNAME,
    STORE_DEVICE_ID,
    STORE_DEVICE_SEQ,
    STORE_MODEL_CODE,
    STORE_PHYSICAL_ID,
)
from lib.navien.airone import AironeDevice
from lib.navien.api import NavienApi
from lib.navien.mqtt import NavienMqtt

# capability -> how to read it from the model. Sensors that come from the air-quality
# REST call read from `unit.air_sensors`; the rest read from the MQTT-reported state.
_SENSOR_KINDS = {
    "measure_temperature": "temperature",
    "measure_humidity": "humidity",
    "measure_pm25": "pm25",
    "measure_co2": "co2",
    "navien_pm1": "pm1",
    "navien_pm10": "pm10",
    "navien_tvoc": "tvoc",
    "navien_radon": "radon",
}


class AironeDevice_(device.Device):

    async def on_init(self) -> None:
        store = self.get_store()
        self._device_seq = store.get(STORE_DEVICE_SEQ)
        self._device_id = str(store.get(STORE_DEVICE_ID) or "")
        self._physical_id = str(store.get(STORE_PHYSICAL_ID) or "")
        self._model_code = int(store.get(STORE_MODEL_CODE) or 0)
        self._language = await compat.ui_language(self.homey)

        self._home_seq = int(await compat.setting_get(self.homey, SETTING_HOME_SEQ) or 0)
        self._api = NavienApi(
            username=await compat.setting_get(self.homey, SETTING_USERNAME),
            password=await compat.setting_get(self.homey, SETTING_PASSWORD),
            log=self.log,
        )
        # A model seeded with our identifiers, so control payloads carry them even
        # before the first report arrives.
        self._unit = AironeDevice(
            device_seq=self._device_seq,
            device_id=self._device_id,
            model_code=self._model_code,
            nickname=self.get_name(),
            physical_device_id=self._physical_id,
        )
        self._mqtt = None
        self._tasks: set = set()
        self._poll_task = None

        for capability, listener in (
            ("onoff", self._on_set_power),
            ("navien_airone_mode", self._on_set_mode),
            ("navien_airone_fan", self._on_set_fan),
            ("navien_airone_option", self._on_set_option),
            ("navien_target_humidity", self._on_set_humidity),
        ):
            if capability in self.get_capabilities():
                self.register_capability_listener(capability, listener)
        self._humidity_range = None

        self.log(f"{self.get_name()} init (seq={self._device_seq}, phys={self._physical_id})")
        self._poll_task = asyncio.create_task(self._run())

    async def on_uninit(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
        if self._mqtt is not None:
            await self._to_thread(self._mqtt.close)

    # --- lifecycle ---------------------------------------------------------

    async def _run(self) -> None:
        """Log in, start MQTT push, then poll REST forever as the fallback."""
        try:
            await self._api.login()
        except Exception as exc:
            self.log(f"login failed: {exc}")
            await self._safe_unavailable("로그인에 실패했습니다. 앱 설정에서 계정을 확인하세요.")
            return

        await self._start_mqtt()
        await self._poll_once(initial=True)
        await self._safe_available()

        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"poll failed: {exc}")

    async def _start_mqtt(self) -> None:
        if not self._api.aws:
            self.log("no AWS credentials; realtime push disabled, polling only")
            return
        loop = asyncio.get_running_loop()
        self._mqtt = NavienMqtt(
            loop=loop,
            user_seq=self._api.user_seq,
            home_seq=self._home_seq,
            creds_provider=lambda: self._api.aws,
            on_reported=self._on_reported,
            log=self.log,
        )
        try:
            await self._to_thread(self._mqtt.connect_blocking)
        except Exception as exc:
            self.log(f"mqtt connect failed (falling back to polling): {exc}")
            self._mqtt = None

    # --- polling / initial state ------------------------------------------

    async def _poll_once(self, initial: bool = False) -> None:
        """Re-read device state and air-quality over REST.

        Also nudges the appliance to publish a fresh MQTT report by sending a `status`
        command — shadow state only arrives on change otherwise, so a just-added device
        would sit empty until first touched.
        """
        for raw in await self._api.list_devices(self._home_seq):
            unit = AironeDevice.from_raw(raw, log=self.log)
            if unit and str(unit.device_id) == self._device_id:
                # `unit.reported` still carries the server's mode metadata (a list),
                # which the MQTT state later overwrites with the current mode (an int),
                # so read the humidity range here before merging.
                await self._sync_humidity_range(unit.humidity_range())
                self._unit.apply_reported(unit.reported)
                break

        try:
            sensors = await self._api.air_sensor(self._device_seq, self._home_seq)
            self._unit.apply_air_sensors(sensors)
        except Exception as exc:
            self.log(f"air-sensor read failed: {exc}")

        if self._unit.is_on or initial:
            await self._request_status()

        await self._apply_state()

    async def _request_status(self) -> None:
        if self._mqtt is None:
            return
        try:
            await self._airone(AIRONE_CMD_STATUS, desired=None)
        except Exception as exc:
            self.log(f"status request failed: {exc}")

    # --- push --------------------------------------------------------------

    def _on_reported(self, device_id: str, reported: dict) -> None:
        """MQTT callback (already marshalled onto the loop)."""
        if device_id and device_id not in (self._device_id, self._physical_id):
            return
        self._unit.apply_reported(reported)
        self._spawn(self._apply_state())

    # --- capability write --------------------------------------------------

    async def _on_set_power(self, value, opts=None):
        await self._airone(AIRONE_CMD_POWER, desired=self._unit.desired_power(bool(value)))
        self._schedule_readback()

    async def _on_set_mode(self, value, opts=None):
        await self._airone(AIRONE_CMD_CHANGE_MODE, desired=self._unit.desired_mode(int(value)))
        self._schedule_readback()

    async def _on_set_fan(self, value, opts=None):
        await self._airone(AIRONE_CMD_CHANGE_MODE, desired=self._unit.desired_fan(int(value)))
        self._schedule_readback()

    async def _on_set_humidity(self, value, opts=None):
        await self._airone(AIRONE_CMD_CHANGE_MODE, desired=self._unit.desired_humidity(int(value)))
        self._schedule_readback()

    async def _on_set_option(self, value, opts=None):
        await self._airone(AIRONE_CMD_CHANGE_MODE, desired=self._unit.desired_option(int(value)))
        self._schedule_readback()

    async def _sync_humidity_range(self, bounds) -> None:
        """Set the target-humidity slider min/max from the server metadata (once)."""
        if bounds == self._humidity_range:
            return
        if "navien_target_humidity" not in self.get_capabilities():
            return
        self._humidity_range = bounds
        low, high = bounds
        try:
            await self.set_capability_options(
                "navien_target_humidity",
                {"min": int(low), "max": int(high), "step": HUMIDITY_STEP},
            )
        except Exception as exc:
            self.log(f"humidity range set failed: {exc}")

    async def _airone(self, command: str, desired):
        client_id = self._mqtt.client_id if self._mqtt else f"rest-U{self._api.user_seq}"
        return await self._api.airone_command(
            device_seq=self._device_seq,
            home_seq=self._home_seq,
            model_code=self._model_code,
            physical_device_id=self._physical_id,
            client_id=client_id,
            command=command,
            desired=desired,
        )

    def _schedule_readback(self) -> None:
        """Re-request status shortly after a command, since we don't apply optimistically."""
        async def readback():
            await asyncio.sleep(AIRONE_READBACK_DELAY_S)
            await self._request_status()
        self._spawn(readback())

    # --- capability read ---------------------------------------------------

    async def _apply_state(self) -> None:
        u = self._unit
        await self._set("onoff", u.is_on)
        await self._set("navien_running_state", self._enum(u.running))
        await self._set("navien_airone_mode", self._enum(u.mode))
        await self._set("navien_airone_fan", self._enum(u.air_volume))
        await self._set("navien_airone_option", self._enum(u.option))
        await self._set("navien_target_humidity", u.target_humidity)
        for capability, kind in _SENSOR_KINDS.items():
            reading = u.air_sensors.get(kind) or {}
            await self._set(capability, self._num(reading.get("value")))
        total = u.air_sensors.get("total") or {}
        await self._set("navien_air_grade", self._enum(total.get("level")))
        filters = u.filters
        await self._set("navien_filter_usage", filters[0] if filters else None)
        await self._set("navien_error_code", self._num(u.error_code))
        await self._set("alarm_generic", u.has_error)

    async def _set(self, capability: str, value) -> None:
        if value is None or capability not in self.get_capabilities():
            return
        try:
            if self.get_capability_value(capability) != value:
                await self.set_capability_value(capability, value)
        except Exception as exc:
            self.log(f"set {capability} failed: {exc}")

    @staticmethod
    def _enum(value):
        return None if value is None else str(value)

    @staticmethod
    def _num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # --- helpers -----------------------------------------------------------

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _to_thread(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

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
