"""One paired Navien AirOne unit.

Logs into the cloud, subscribes to the realtime MQTT push for fresh state, and falls
back to a slow REST re-read (which also carries the air-quality readings MQTT does not).
Control is REST: a capability change posts a command and the device's own reply, pushed
back over MQTT, is what updates the capability — so the UI reflects the appliance, not
an optimistic guess. Air-quality is REST-only.

State is deep-merged, never replaced, because reports arrive partial. See docs/PORTING.md.
"""

import asyncio
import time

from homey import device

from navien_lib import compat
from navien_lib.const import (
    AIRONE_CMD_CHANGE_MODE,
    AIRONE_CMD_POWER,
    AIRONE_CMD_STATUS,
    AIRONE_READBACK_DELAY_S,
    FAN_ADJUSTABLE_MODES,
    HUMIDITY_STEP,
    MODES_WITH_HUMIDITY,
    OPTION_SAVER,
    OPTION_SLEEP,
    OPTION_TURBO,
    POLL_INTERVAL_S,
    SETTING_HOME_SEQ,
    STORE_DEVICE_ID,
    STORE_DEVICE_SEQ,
    STORE_MODEL_CODE,
    STORE_PHYSICAL_ID,
)
from navien_lib.navien.airone import AironeDevice
from navien_lib.navien.mqtt import NavienMqtt

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

# Text "grade" sensors that surface the server's air-quality level ("좋음"/"나쁨"/…)
# alongside the numeric reading, for the sensors that carry one.
_GRADE_KINDS = {
    "navien_tvoc_grade": "tvoc",
    "navien_radon_grade": "radon",
}

# The mode/fan pickers fold "option" (수면/터보/절전) into the two lists, so a single
# picker value maps to either a mode/airVolume or an option. Ids: mode "m{mode}" +
# "sleep"; fan "v{airVolume}" + "o{option}". Kept in sync with the capability JSONs.
_MODE_IDS = {"m12", "m10", "m4", "m9", "m8", "m6", "sleep", "m17"}
_FAN_IDS = {"v4", "o3", "v1", "v3", "o2"}

# The appliance takes a few seconds to accept a command and report the new state, so a
# poll/MQTT report arriving in that gap still carries the *old* value and would snap a
# just-changed control back. After a command we show the requested value immediately
# (optimistic) and hold it — ignoring control updates — until the device settles.
_SETTLE_S = 5.0


def _mode_id(u) -> str | None:
    """Current mode as a picker id — 수면 option wins over the underlying mode."""
    if u.option == OPTION_SLEEP:
        return "sleep"
    if u.mode is None:
        return None
    return f"m{u.mode}"


def _fan_id(u) -> str | None:
    """Current fan as a picker id — 절전/터보 option wins over the raw airVolume."""
    if u.option in (OPTION_TURBO, OPTION_SAVER):
        return f"o{u.option}"
    if u.air_volume is None:
        return None
    return f"v{u.air_volume}"


class AironeDevice_(device.Device):

    async def on_init(self) -> None:
        store = self.get_store()
        self._device_seq = store.get(STORE_DEVICE_SEQ)
        self._device_id = str(store.get(STORE_DEVICE_ID) or "")
        self._physical_id = str(store.get(STORE_PHYSICAL_ID) or "")
        self._model_code = int(store.get(STORE_MODEL_CODE) or 0)
        self._language = await compat.ui_language(self.homey)

        self._home_seq = int(await compat.setting_get(self.homey, SETTING_HOME_SEQ) or 0)
        # The Navien session is shared app-wide (one session per account), acquired in
        # _run. Kept as None until then.
        self._api = None
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
            ("navien_target_humidity", self._on_set_humidity),
        ):
            if capability in self.get_capabilities():
                self.register_capability_listener(capability, listener)
        self._humidity_range = None
        # Remembered so the humidity slider stays visible (with its title) even in modes
        # that don't control humidity — attempts to change it there are rejected with a
        # toast rather than the slider vanishing.
        self._last_humidity = None
        # monotonic deadline until which control capabilities are held at their optimistic
        # value (see _SETTLE_S).
        self._settle_until = 0.0

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
        await self._acquire_api()
        await self._start_mqtt()
        try:
            await self._poll_once(initial=True)
        except Exception as exc:
            self.log(f"initial poll failed: {exc}")
        await self._safe_available()

        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                await self._poll_once()
                await self._ensure_mqtt()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"poll failed: {exc}")

    async def _acquire_api(self) -> None:
        """Get the app-wide shared Navien session, retrying rather than giving up.

        One session per account means devices must not each hold their own login, so
        the app owns it (`shared_api`). A transient failure (e.g. a 403 while the phone
        app is holding the session) just backs off and retries — the device stays
        unavailable until it's in.
        """
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

    async def _ensure_mqtt(self) -> None:
        """Keep realtime push alive: reconnect with fresh credentials if it dropped.

        paho reconnects transient blips on its own, but the presigned WebSocket path
        expires, so a lasting drop needs a fresh login (which re-mints the AWS creds
        via secured-sign-in) and a clean reconnect.
        """
        if self._mqtt is None:
            await self._start_mqtt()
            return
        if self._mqtt.connected:
            return
        self.log("mqtt not connected; refreshing credentials and reconnecting")
        try:
            await self._api.login()
            await self._to_thread(self._mqtt.close)
            await self._to_thread(self._mqtt.connect_blocking)
        except Exception as exc:
            self.log(f"mqtt reconnect failed: {exc}")

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
        wants_sensors = True
        for raw in await self._api.list_devices(self._home_seq):
            unit = AironeDevice.from_raw(raw, log=self.log)
            if unit and str(unit.device_id) == self._device_id:
                # Refresh the model code (control-topic addressing) from the live list,
                # so a device paired before the model-code fix corrects itself.
                if unit.model_code:
                    self._model_code = unit.model_code
                # Only the humidity *range* is taken from the device-list metadata. Its
                # roomController.mode is a list (not the live int) and it carries no
                # airVolume/option/running, so merging it into the live state would clobber
                # what MQTT reported — the status request below refreshes the live values.
                await self._sync_humidity_range(unit.humidity_range())
                wants_sensors = unit.wants_air_sensors()
                break

        if wants_sensors:
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
        desired = self._unit.desired_power(bool(value))
        await self._airone(AIRONE_CMD_POWER, desired=desired)
        await self._optimistic(desired)

    async def _on_set_mode(self, value, opts=None):
        # "m{mode}" picks a mode; "sleep" applies the sleep option to the current mode.
        v = str(value)
        if v == "sleep":
            desired = self._unit.desired_option(OPTION_SLEEP)
        else:
            desired = self._unit.desired_mode(int(v[1:]))
        await self._airone(AIRONE_CMD_CHANGE_MODE, desired=desired)
        await self._optimistic(desired)

    async def _on_set_fan(self, value, opts=None):
        # "v{airVolume}" picks a fan speed; "o{option}" picks turbo/saver.
        if not self._fan_allowed():
            raise Exception(
                f"{self._mode_label()} 모드에서는 풍량을 조절할 수 없습니다. "
                f"환기·제습·청정·숙면·바이패스 모드에서 조절하세요."
            )
        v = str(value)
        if v.startswith("o"):
            desired = self._unit.desired_option(int(v[1:]))
        else:
            desired = self._unit.desired_fan(int(v[1:]))
        await self._airone(AIRONE_CMD_CHANGE_MODE, desired=desired)
        await self._optimistic(desired)

    async def _on_set_humidity(self, value, opts=None):
        # Raising surfaces the message as a toast in the app and reverts the slider.
        if self._unit.option in (OPTION_TURBO, OPTION_SAVER):
            raise Exception("터보·절전에서는 습도가 자동이라 조절할 수 없습니다.")
        if not self._humidity_allowed():
            raise Exception(
                f"{self._mode_label()} 모드에서는 희망습도를 조절할 수 없습니다. "
                f"제습 모드에서만 조절할 수 있습니다."
            )
        desired = self._unit.desired_humidity(int(value))
        await self._airone(AIRONE_CMD_CHANGE_MODE, desired=desired)
        await self._optimistic(desired)

    # --- command validation + optimistic settle ---------------------------------

    def _mode_label(self) -> str:
        return self._unit.mode_name(self._language) or "현재"

    def _humidity_allowed(self) -> bool:
        """희망습도는 제습 모드에서만 조절할 수 있다."""
        return self._unit.mode in MODES_WITH_HUMIDITY

    def _fan_allowed(self) -> bool:
        """풍량은 환기·제습·청정·바이패스 모드, 또는 숙면 옵션일 때만 조절할 수 있다."""
        return self._unit.option == OPTION_SLEEP or self._unit.mode in FAN_ADJUSTABLE_MODES

    async def _optimistic(self, desired) -> None:
        """Reflect a just-sent command right away and hold it for a few seconds.

        The appliance needs ~3 s to accept the command and report back; until then its
        reports still carry the old value. We merge the requested state into the model,
        push it now, and set a settle deadline so incoming reports can't snap the control
        back in the meantime. A readback nudges a fresh report once it has settled.
        """
        self._unit.apply_reported(desired)
        self._settle_until = time.monotonic() + _SETTLE_S
        self._schedule_readback()
        await self._apply_state(force=True)

        async def reapply():
            # After the window, push the confirmed (or, if the command was rejected,
            # reverted) state so a failed command doesn't leave a stale optimistic value.
            await asyncio.sleep(_SETTLE_S + 0.5)
            await self._apply_state()
        self._spawn(reapply())

    # --- flow-card entry points -------------------------------------------

    async def flow_set_mode(self, mode_id: str) -> None:
        await self._on_set_mode(mode_id)

    async def flow_set_fan(self, fan_id: str) -> None:
        await self._on_set_fan(fan_id)

    async def flow_set_power(self, on: bool) -> None:
        await self._on_set_power(on)

    async def flow_set_humidity(self, value: int) -> None:
        await self._on_set_humidity(value)

    def flow_mode_id(self):
        return _mode_id(self._unit)

    def flow_fan_id(self):
        return _fan_id(self._unit)

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
        """Nudge the appliance to publish a fresh report shortly after a command."""
        async def readback():
            await asyncio.sleep(AIRONE_READBACK_DELAY_S)
            await self._request_status()
        self._spawn(readback())

    # --- capability read ---------------------------------------------------

    async def _apply_state(self, force: bool = False) -> None:
        u = self._unit
        # Read-only reflections of the appliance's own state — always applied so a change
        # (e.g. entering '자동 건조중') shows immediately, never held by the settle window.
        await self._set("navien_running_state", u.running_name(self._language))
        await self._set("navien_airone_status", u.status_text(self._language))
        # User-set controls (power/mode/fan/humidity) are held at the value the user just
        # set until the appliance settles, so a lagging report can't snap them back.
        # `force` is the optimistic push right after a command.
        if force or time.monotonic() >= self._settle_until:
            await self._set("onoff", u.is_on)
            await self._set_choice("navien_airone_mode", _mode_id(u), _MODE_IDS)
            await self._set_choice("navien_airone_fan", _fan_id(u), _FAN_IDS)
            hum = u.target_humidity
            if hum is not None:
                self._last_humidity = hum
            # Keep the slider populated (so it shows its title) even outside 제습 mode,
            # falling back to the mid-point of the allowed band.
            if self._last_humidity is None:
                low, high = u.humidity_range()
                self._last_humidity = (low + high) // 2
            await self._set("navien_target_humidity",
                            hum if hum is not None else self._last_humidity)
        for capability, kind in _SENSOR_KINDS.items():
            reading = u.air_sensors.get(kind) or {}
            await self._set(capability, self._num(reading.get("value")))
        for capability, kind in _GRADE_KINDS.items():
            reading = u.air_sensors.get(kind) or {}
            await self._set(capability, reading.get("level") or None)
        total = u.air_sensors.get("total") or {}
        await self._set("navien_air_grade", self._num(total.get("value")))
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

    async def _set_choice(self, capability: str, value, valid: set) -> None:
        """Set an enum picker only when the value is one the picker offers; an
        unknown id would make Homey reject the whole capability update."""
        if value not in valid:
            return
        await self._set(capability, value)

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
