"""One paired Navien sleep mat.

Logs in, rebuilds the mat model from the device list (for structure: zones, type,
ranges), subscribes to the mat's AWS shadow over MQTT for realtime state, and falls
back to a slow REST re-read. Control is REST → shadow; state comes back over MQTT, so
nothing is applied optimistically. Four-season temperature ranges follow the active
(heat/cool) control and are re-pushed to Homey when the season changes.
"""

import asyncio

from homey import device

from navien_lib import compat
from navien_lib.const import (
    POLL_INTERVAL_S,
    SERVICE_MATE,
    SETTING_HOME_SEQ,
    STORE_DEVICE_ID,
    STORE_DEVICE_SEQ,
    STORE_MODEL_CODE,
    ZONE_SINGLE,
)
from navien_lib.navien.mate import MateDevice, extract_mate_reported
from navien_lib.navien.mqtt import NavienMqtt


def _split(cap: str):
    base, _, inst = cap.partition(".")
    return base, (inst or ZONE_SINGLE)


class MateDevice_(device.Device):

    async def on_init(self) -> None:
        store = self.get_store()
        self._device_seq = store.get(STORE_DEVICE_SEQ)
        self._device_id = str(store.get(STORE_DEVICE_ID) or "")
        self._model_code = int(store.get(STORE_MODEL_CODE) or 0)
        self._language = await compat.ui_language(self.homey)
        self._home_seq = int(await compat.setting_get(self.homey, SETTING_HOME_SEQ) or 0)
        # Shared app-wide session (one per account); acquired in _run.
        self._api = None
        self._mat = None
        self._mqtt = None
        self._tasks: set = set()
        self._poll_task = None
        self._last_range = None

        for cap in self.get_capabilities():
            base, zone = _split(cap)
            listener = None
            if base == "onoff":
                listener = self._on_set_power
            elif base == "target_temperature":
                listener = self._make_temp(zone)
            elif base == "navien_heat_level":
                listener = self._make_level(zone)
            elif base == "navien_season":
                listener = self._on_set_season
            if listener is not None:
                self.register_capability_listener(cap, listener)

        self.log(f"{self.get_name()} init (seq={self._device_seq})")
        self._poll_task = asyncio.create_task(self._run())

    async def on_uninit(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
        if self._mqtt is not None:
            await self._to_thread(self._mqtt.close)

    # --- lifecycle ---------------------------------------------------------

    async def _run(self) -> None:
        await self._acquire_api()
        await self._refresh_model()
        await self._start_mqtt()
        await self._request_initial()
        await self._apply_state()
        await self._safe_available()

        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                await self._refresh_model()
                await self._apply_state()
                await self._ensure_mqtt()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"poll failed: {exc}")

    async def _ensure_mqtt(self) -> None:
        """Reconnect realtime push with fresh credentials if it dropped."""
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

    async def _refresh_model(self) -> None:
        for raw in await self._api.list_devices(self._home_seq):
            if str(raw.get("deviceId")) != self._device_id:
                continue
            mat = MateDevice.from_raw(raw, log=self.log)
            if mat is None:
                return
            if self._mat is not None:
                mat.reported = self._mat.reported  # keep MQTT-accumulated state
            self._mat = mat
            return

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
            prefixes=("mate",),
            parser=extract_mate_reported,
            log=self.log,
        )
        try:
            await self._to_thread(self._mqtt.connect_blocking)
        except Exception as exc:
            self.log(f"mqtt connect failed (falling back to polling): {exc}")
            self._mqtt = None

    async def _request_initial(self) -> None:
        """Nudge the mat to publish its current shadow state (empty desired)."""
        try:
            await self._mate({})
        except Exception as exc:
            self.log(f"initial-state request failed: {exc}")

    # --- push --------------------------------------------------------------

    def _on_reported(self, device_id: str, reported: dict) -> None:
        if self._mat is None:
            return
        if device_id and device_id != self._device_id:
            return
        self._mat.apply_reported(reported)
        self._spawn(self._apply_state())

    async def _acquire_api(self) -> None:
        """Get the app-wide shared Navien session, retrying rather than giving up
        (one session per account — see the AirOne device for the rationale)."""
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

    # --- control -----------------------------------------------------------

    async def _on_set_power(self, value, opts=None):
        await self._mate(self._mat.desired_power(bool(value)))

    def _make_temp(self, zone):
        async def listener(value, opts=None):
            await self._mate(self._mat.desired_temperature(zone, value))
        return listener

    def _make_level(self, zone):
        async def listener(value, opts=None):
            await self._mate(self._mat.desired_level(zone, value))
        return listener

    async def _on_set_season(self, value, opts=None):
        await self._mate(self._mat.desired_season(int(value)))

    # --- flow-card entry points -------------------------------------------

    def _require_zone(self, zone: str) -> None:
        if self._mat is None:
            raise Exception("기기 정보를 아직 불러오지 못했습니다.")
        if zone not in self._mat.zones:
            raise Exception("이 매트에는 해당 구역이 없습니다.")

    async def flow_set_power(self, on: bool) -> None:
        await self._on_set_power(on)

    async def flow_set_season(self, season: int) -> None:
        if self._mat is None or not self._mat.is_four_season:
            raise Exception("사계절 매트가 아니라 계절을 바꿀 수 없습니다.")
        await self._on_set_season(season)

    async def flow_set_temperature(self, zone: str, value) -> None:
        hc = self._mat.heat_control if self._mat else None
        if not (hc and hc.is_celsius):
            raise Exception("온도(℃)로 조절하는 매트가 아닙니다.")
        self._require_zone(zone)
        await self._mate(self._mat.desired_temperature(zone, value))

    async def flow_set_level(self, zone: str, value) -> None:
        hc = self._mat.heat_control if self._mat else None
        if not (hc and hc.is_level):
            raise Exception("단계로 조절하는 매트가 아닙니다.")
        self._require_zone(zone)
        await self._mate(self._mat.desired_level(zone, value))

    def flow_is_on(self) -> bool:
        return bool(self._mat and self._mat.is_on)

    def flow_season(self):
        return None if self._mat is None else self._mat.season

    async def _mate(self, desired):
        if self._mat is None:
            raise Exception("기기 정보를 아직 불러오지 못했습니다.")
        return await self._api.mate_control(
            device_seq=self._device_seq,
            home_seq=self._home_seq,
            device_id=self._device_id,
            model_code=self._model_code,
            service_code=SERVICE_MATE,
            desired=desired,
        )

    # --- read --------------------------------------------------------------

    async def _apply_state(self) -> None:
        m = self._mat
        if m is None:
            return
        caps = self.get_capabilities()
        await self._set("onoff", m.is_on)
        await self._set_str("navien_operation_mode", m.mode_name(self._language))
        await self._set("navien_error_code", self._num(m.error_code))
        await self._set("alarm_generic", m.has_error)
        await self._set("alarm_heat", m.over_safe_value)
        await self._set("navien_season", m.season_id())
        for cap in caps:
            base, zone = _split(cap)
            if base == "target_temperature":
                await self._set(cap, self._num(m.zone_setting(zone)))
            elif base == "measure_temperature":
                await self._set(cap, self._num(m.zone_current(zone)))
            elif base == "navien_heat_level":
                await self._set(cap, self._num(m.zone_setting(zone)))
        await self._sync_ranges()

    async def _sync_ranges(self) -> None:
        """Re-push target-temperature min/max when the four-season range changes."""
        m = self._mat
        active = m.active_control if m else None
        if not active or not active.is_celsius:
            return
        key = (active.range_min, active.range_max)
        if key == self._last_range:
            return
        self._last_range = key
        opts = {"min": active.range_min or 20, "max": active.range_max or 45, "step": 0.5}
        for cap in self.get_capabilities():
            if _split(cap)[0] == "target_temperature":
                try:
                    await self.set_capability_options(cap, opts)
                except Exception:
                    pass

    async def _set(self, capability: str, value) -> None:
        if value is None or capability not in self.get_capabilities():
            return
        try:
            if self.get_capability_value(capability) != value:
                await self.set_capability_value(capability, value)
        except Exception as exc:
            self.log(f"set {capability} failed: {exc}")

    async def _set_str(self, capability: str, text) -> None:
        if text is None:
            return
        await self._set(capability, str(text))

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
