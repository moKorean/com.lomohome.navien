"""One paired Navien AirMonitor.

Reads air quality from the parent AirOne's `/air-sensor` REST endpoint (air quality is
REST-only — MQTT carries the sensor kinds but not their values) and applies the reading
for this monitor's zone to standard/custom measure capabilities. Read-only, no MQTT.
"""

import asyncio
import random

from homey import device

from navien_lib import compat
from navien_lib.const import (
    MQTT_BACKOFF_S,
    POLL_INTERVAL_S,
    POLL_JITTER,
    POLL_START_JITTER,
    SETTING_HOME_SEQ,
    STORE_DEVICE_SEQ,
    STORE_MONITOR_ID,
    STORE_ZONE_ID,
)
from navien_lib.navien.airone import air_sensor_changes, parse_air_sensors_for

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
        # Gate 0 Q6: a home_seq of 0 still makes a *valid* MQTT topic filter elsewhere in
        # the app, so a wrong value fails silently. Log what this device resolved.
        self.log(f"navien: home_seq={self._home_seq} for {self.get_name()}")
        # The Navien session is shared app-wide (one session per account); acquired in
        # _run.
        self._api = None
        self._sensors: dict = {}
        # Set on teardown so the poll task's done-callback can tell "died" from
        # "dismantled"; see _on_poll_task_done.
        self._closing = False
        # Backoff walk for restarting a poll task that died on its own.
        self._restart_step = 0
        self._restart_delay = MQTT_BACKOFF_S[0]
        # REST is this monitor's only link, so consecutive failed reads are its only
        # availability signal. A count rather than a last-success timestamp: the verdict
        # below is written in cycles, so a second, time-based expression of the same fact
        # would only be something that could disagree with it.
        self._rest_failures = 0
        self._poll_task = asyncio.create_task(self._run())
        self._poll_task.add_done_callback(self._on_poll_task_done)

    async def on_uninit(self) -> None:
        await self._teardown()

    async def _teardown(self) -> None:
        """The AirMonitor's variant of the shared teardown.

        Written per-device on purpose: this module is "Read-only, no MQTT" (see the module
        docstring), so it has neither `self._mqtt` nor a reconnect task nor a `_tasks` set,
        and the AirOne/mat form would raise AttributeError here. What carries over is the
        part that matters — cancel, then *await*, so nothing is still running when Homey
        considers the device gone.
        """
        self._closing = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)

    def _on_poll_task_done(self, task) -> None:
        """Restart the poll loop if it died, and stay out of the way if it was torn down.

        Both omissions here would be bugs. `on_uninit` cancels without awaiting and the
        cancellation lands on the bare `asyncio.sleep` outside the try, so a dismantled
        task also ends up here — only `task.cancelled()` separates the two cases.
        And `_poll_task` must be reassigned: otherwise a later `on_uninit` cancels the
        dead original, the restarted loop outlives the device, and it goes on calling
        `set_capability_value` on a torn-down Device.
        """
        if task.cancelled() or self._closing:
            return
        exc = task.exception()
        self.log(f"navien: poll task died ({exc!r}); restarting in {self._restart_delay}s")
        self._poll_task = asyncio.create_task(self._restart_poll())
        self._poll_task.add_done_callback(self._on_poll_task_done)

    async def _restart_poll(self) -> None:
        """Wait one MQTT_BACKOFF_S step, then re-enter `_run`.

        Every restart is logged with its exception (see the caller) — a line that keeps
        repeating is the signal that a real crash is hiding inside this loop.
        """
        await asyncio.sleep(self._restart_delay)
        self._restart_step = min(self._restart_step + 1, len(MQTT_BACKOFF_S) - 1)
        self._restart_delay = MQTT_BACKOFF_S[self._restart_step]
        await self._run()

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
        # Guarded as one block. Neither call was guarded, `_http` only caught HTTPError,
        # and so a single URLError at boot escaped `_run` before the loop below was ever
        # reached — this monitor then polled never again until the app restarted. AirOne
        # already guards the same sequence (airone/device.py:153-156); that asymmetry was
        # the bug.
        try:
            await self._acquire_api()
            await self._poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"initial poll failed: {exc}")
        await self._safe_available()
        # The boot attempt is deliberately outside the matrix's two-cycle budget: it is
        # already excused by the unconditional `_safe_available()` above, so counting it
        # would make the first real cycle the second strike.
        self._rest_failures = 0
        # One-shot 0-30 s offset (POLL_START_JITTER of the tick) on the first loop sleep
        # only, so the three device types stop ticking in lockstep from boot.
        offset = POLL_INTERVAL_S * random.uniform(0.0, POLL_START_JITTER)
        while True:
            delay = POLL_INTERVAL_S * random.uniform(1 - POLL_JITTER, 1 + POLL_JITTER) + offset
            offset = 0.0
            self.log(f"navien: next poll in {delay:.0f}s")
            await asyncio.sleep(delay)
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"poll failed: {exc}")

    async def _poll_once(self) -> None:
        try:
            sensors = await self._api.air_sensor(self._parent_seq, self._home_seq)
        except Exception:
            self._rest_failures += 1
            await self._update_availability()
            raise
        # Reset on an explicit success, never inferred from the absence of an exception.
        self._rest_failures = 0
        parsed = parse_air_sensors_for(sensors, self._zone_id, self._monitor_id)
        # Deliberate instrumentation for the open question in `_update_availability`:
        # changed readings only, so a live monitor is one short line per poll and a feed
        # that has stopped moving shows up as "unchanged" repeating. The AirOne logs the
        # same line from the same endpoint, which is what makes the two comparable.
        changed = air_sensor_changes(self._sensors, parsed)
        self.log(f"navien: air-sensor {changed or 'unchanged'}")
        if parsed:  # merge — an empty poll must not wipe the values
            self._sensors.update(parsed)
        await self._apply()
        await self._update_availability()

    async def _update_availability(self) -> None:
        """`set_unavailable` on two consecutive failed polls, `set_available` on recovery.

        This is the one device where P3 comes out the other way, and the asymmetry is the
        principle working rather than an inconsistency. The AirOne keeps a failing link
        *available* because control still reaches it and greying the tile out would remove
        working buttons. Here `/air-sensor` is the only link there is, the driver registers
        no capability listener at all, and `_poll_once` merges rather than replaces — so a
        permanent failure would otherwise leave yesterday's PM2.5 on screen with nothing to
        say it is yesterday's. REST failure simply *is* unavailability here, there is no
        control to lose by saying so, and with no free-text capability the AirOne's marker
        has nowhere to go anyway.

        OPEN — should this monitor follow its parent AirOne's `connected` flag?
        The monitor has its own power, but it may communicate *through* the AirOne: the
        `/air-sensor` endpoint is served by the cloud against the **AirOne's** `device_seq`,
        which is the only seq this device has (`_parent_seq`). On 2026-08-03 the AirOne was
        unplugged and that endpoint still answered 200 — which proves nothing either way,
        because the cloud can serve last-known values for a monitor that has gone quiet.
        The consequence if it does go quiet: this device stays *available* and keeps showing
        a frozen reading with nothing to say it is frozen, which is the exact failure the
        two-cycle rule above exists to prevent, arriving through a door it does not watch.
        HOW TO SETTLE IT: the `navien: air-sensor …` line logged in `_poll_once` (the AirOne
        logs the same line off the same endpoint). Unplug the AirOne and read a few cycles.
        If the values keep moving, the monitor is independent and nothing changes here. If
        they freeze, this device should follow the parent's `connected` flag and go
        unavailable with it, and the flag would have to be plumbed from the AirOne's device
        (there is no per-monitor `connected` in `GET /devices`). Not implemented on purpose:
        the evidence does not exist yet, and guessing wrong greys out a working sensor.
        """
        if self._rest_failures >= 2:
            await self._safe_unavailable("공기질 데이터를 가져올 수 없습니다")
        else:
            await self._safe_available()

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
