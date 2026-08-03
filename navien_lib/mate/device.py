"""One paired Navien sleep mat.

Logs in, rebuilds the mat model from the device list (for structure: zones, type,
ranges), subscribes to the mat's AWS shadow over MQTT for realtime state, and falls
back to a slow REST re-read. Control is REST → shadow; state comes back over MQTT, so
nothing is applied optimistically. Four-season temperature ranges follow the active
(heat/cool) control and are re-pushed to Homey when the season changes.
"""

import asyncio
import random

from homey import device

from navien_lib import compat
from navien_lib.const import (
    INITIAL_STATE_TIMEOUT_S,
    MQTT_BACKOFF_S,
    POLL_INTERVAL_S,
    POLL_JITTER,
    POLL_START_JITTER,
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
        # Gate 0 Q6: a home_seq of 0 still makes a *valid* MQTT topic filter, so the
        # connection succeeds and no frame ever arrives. Log what this device resolved.
        self.log(f"navien: home_seq={self._home_seq} for {self.get_name()}")
        # Shared app-wide session (one per account); acquired in _run.
        self._api = None
        self._mat = None
        self._mqtt = None
        # B3, same shape and same reason as the AirOne's: the poll task (`_ensure_mqtt`,
        # `_sync_home_seq`), the reconnect task and the missing-credentials retry all
        # mutate `self._mqtt` with `await` points in between, and `_ensure_mqtt`'s
        # `connected` early-out is inert exactly while a reconnect is in flight. Every
        # close/connect_blocking pair runs under this lock, `_to_thread` hops included.
        self._mqtt_lock = asyncio.Lock()
        self._tasks: set = set()
        self._poll_task = None
        self._last_range = None
        # Set on teardown so the poll task's done-callback can tell "died" from
        # "dismantled"; see _on_poll_task_done.
        self._closing = False
        # Backoff walk for restarting a poll task that died on its own.
        self._restart_step = 0
        self._restart_delay = MQTT_BACKOFF_S[0]
        # Backoff walk for retrying MQTT start-up when the session carries no AWS
        # credentials; see _schedule_mqtt_retry.
        self._mqtt_retry_task = None
        self._mqtt_retry_step = 0
        # Event-driven reconnect, same shape as the AirOne's (F3).
        self._reconnect_task = None
        self._backoff_index = 0
        # The REST arm of the availability matrix, in full: consecutive failed device-list
        # reads, counted rather than timestamped because the verdict is written in cycles.
        # The mat gets no staleness marker — see `_update_availability` for why that is a
        # decision and not an omission.
        self._rest_failures = 0
        # Reports seen since boot. A plain counter rather than the AirOne's timestamp,
        # because the only thing the mat needs it for is the post-connect watchdog —
        # without a staleness marker there is nothing to measure an *age* against.
        self._reports = 0

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
        self._poll_task.add_done_callback(self._on_poll_task_done)

    async def on_uninit(self) -> None:
        await self._teardown()

    async def _teardown(self) -> None:
        """Cancel every task this device owns, wait for them, then close MQTT (M5).

        The await is what makes the ordering real for the asyncio half: `.cancel()` returns
        immediately, so without it `close()` runs while `_run` may still be mid-cycle, and a
        `_run` that reaches `_ensure_mqtt` first rebuilds the client that was just torn
        down — the device raising its own MQTT connection during teardown.

        It does nothing for the executor threads, though, and `_to_thread(close)` /
        `_to_thread(connect_blocking)` are the only blocking calls those tasks make: the
        gather can return with a `connect_blocking` still running on a pool worker. The
        lock below stops any cancelled-but-still-running coroutine from being mid-sequence;
        NavienMqtt refusing to install (or unwinding) a client while `_closing` is set is
        what covers the thread itself.
        """
        self._closing = True
        pending = [t for t in (self._poll_task, self._reconnect_task, self._mqtt_retry_task)
                   if t is not None]
        # Snapshotted: the tasks' own done-callbacks discard from this set as they finish.
        pending += list(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # After the cancellations, never before: a task holding the lock has to be
        # cancelled to release it, and it cannot be cancelled by a teardown parked on it.
        async with self._mqtt_lock:
            if self._mqtt is not None:
                await self._to_thread(self._mqtt.close)

    # --- lifecycle ---------------------------------------------------------

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

    async def _run(self) -> None:
        # Guarded as one block, not per call. None of these four was guarded, `_http`
        # only caught HTTPError, and so a single URLError at boot escaped `_run` before
        # the loop below was ever reached — the mat then polled never again until the app
        # restarted. Guarding only `_refresh_model` would leave the other three as live
        # escape routes. AirOne already guards the same sequence (airone/device.py:153-156);
        # that asymmetry was the bug.
        try:
            await self._acquire_api()
            await self._refresh_model()
            await self._start_mqtt()
            await self._request_initial()
            await self._apply_state()
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
        # only, so the device types stop ticking in lockstep from boot without delaying
        # this mat's own start-up.
        offset = POLL_INTERVAL_S * random.uniform(0.0, POLL_START_JITTER)
        while True:
            delay = POLL_INTERVAL_S * random.uniform(1 - POLL_JITTER, 1 + POLL_JITTER) + offset
            offset = 0.0
            self.log(f"navien: next poll in {delay:.0f}s")
            await asyncio.sleep(delay)
            try:
                await self._sync_home_seq()
                await self._refresh_model()
                await self._apply_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"poll failed: {exc}")
            await self._update_availability()
            # REST and MQTT are independent links, so they get independent guards. Sharing
            # one `try` meant a single failed REST read cancelled that cycle's reconnect
            # check, and the push link stayed down for another full interval for no reason.
            try:
                await self._ensure_mqtt()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"mqtt check failed: {exc}")

    async def _ensure_mqtt(self) -> None:
        """Reconnect realtime push with fresh credentials if it dropped.

        Under `_mqtt_lock` (B3). The `connected` early-out is not a substitute for it: a
        reconnect in flight leaves the client *not* connected, so the check waves this
        cycle straight through into a second close/connect on the same object.
        """
        async with self._mqtt_lock:
            if self._mqtt is None:
                await self._start_mqtt_locked()
                return
            if self._mqtt.connected:
                return
            self.log("mqtt not connected; refreshing credentials and reconnecting")
            try:
                # This path never went through `_authed`, so it was the one place where
                # every device re-logged in on its own schedule — and on this account each
                # login invalidates the previous session, so N devices reconnecting
                # together used to bounce each other. The generation is captured now and
                # handed to the session: if a sibling already minted credentials seconds
                # ago, this one reuses them.
                await self._api.login_if_stale(self._api.auth_gen)
                await self._to_thread(self._mqtt.close)
                await self._to_thread(self._mqtt.connect_blocking)
            except Exception as exc:
                self.log(f"mqtt reconnect failed: {exc}")

    async def _sync_home_seq(self) -> None:
        """Pick up a home_seq the settings page rewrote, and resubscribe if it changed.

        `on_init` read this once, and `save_credentials` overwrites it whenever the account
        is re-entered, so a device could spend the rest of its life addressing the wrong
        home. Re-reading it is only half the fix: the topic filter is chosen inside
        `_on_connect`, so an already-connected client stays subscribed to the old tree no
        matter what this attribute says. Dropping the client is what makes `_ensure_mqtt`
        build a new one, and the provider then hands it the new filter.
        """
        raw = await compat.setting_get(self.homey, SETTING_HOME_SEQ)
        try:
            home_seq = int(raw or 0)
        except (TypeError, ValueError):
            return
        if home_seq == self._home_seq:
            return
        self.log(f"navien: home_seq changed {self._home_seq} -> {home_seq}; resubscribing")
        self._home_seq = home_seq
        # Under the lock (B3): dropping the client is a mutation like any other, and a
        # reconnect running at the same moment would otherwise close and rebuild the very
        # object this is discarding.
        async with self._mqtt_lock:
            if self._mqtt is not None:
                await self._to_thread(self._mqtt.close)
                self._mqtt = None

    async def _update_availability(self) -> None:
        """The REST arm of the availability matrix, and only that arm.

        The mat gets no staleness marker, deliberately. Its nearest free-text capability is
        `navien_operation_mode`, written from `mode_name()` — a plain localized name that
        users string-compare in Flow automations (navien/airone.py:317-318 contrasts
        exactly this kind of sensor with the AirOne's composite status line for that
        reason). Appending " · 최신 아님" to it would break running automations in order to
        report a display problem, which is a strictly worse trade than staying quiet.
        """
        if self._rest_failures >= 2:
            await self._safe_unavailable("나비엔 서버에 연결할 수 없습니다")
        else:
            await self._safe_available()

    async def _refresh_model(self) -> None:
        try:
            devices = await self._api.list_devices(self._home_seq)
        except Exception:
            self._rest_failures += 1
            raise
        # Reset on an explicit success and never inferred from the absence of an exception:
        # after F2 the sub-calls around this one guard themselves, so "the poll did not
        # throw" no longer says anything about the REST link.
        self._rest_failures = 0
        for raw in devices:
            if str(raw.get("deviceId")) != self._device_id:
                continue
            mat = MateDevice.from_raw(raw, log=self.log)
            if mat is None:
                return
            if self._mat is not None:
                mat.reported = self._mat.reported  # keep MQTT-accumulated state
            self._mat = mat
            return

    # --- event-driven reconnect (F3) ---------------------------------------

    def _on_mqtt_connected(self) -> None:
        """The push link came up. Already marshalled onto the loop by NavienMqtt.

        Deliberately does *not* reset `_backoff_index` — see `_after_connect`. A flapping
        link reaches this callback on every flap, so resetting here turned the backoff into
        a fixed 5 s retry with a full two-step `login()` behind each attempt.
        """
        if self._closing:
            return
        self._spawn(self._after_connect())

    def _on_mqtt_disconnected(self) -> None:
        """The push link went down. Already marshalled onto the loop by NavienMqtt."""
        if self._closing:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Walk MQTT_BACKOFF_S until the push link is back, saturating at 300 s.

        F3: paho retries the one presigned path it was handed, and that path carries an STS
        token nothing inside paho can refresh — so a lasting drop needs a fresh login out
        here. Before this, that only happened on the 300 s poll tick.
        """
        # A local counter, not `_backoff_index`: the index saturates at the last step, so
        # reusing it would print "attempt 6" forever and hide how long the link has been down.
        attempt = 0
        while not self._closing:
            attempt += 1
            delay = MQTT_BACKOFF_S[self._backoff_index]
            self._backoff_index = min(self._backoff_index + 1, len(MQTT_BACKOFF_S) - 1)
            self.log(f"navien mqtt: reconnect attempt {attempt} in {delay}s")
            await asyncio.sleep(delay)
            if self._closing:
                return
            ok = False
            try:
                # B3: held across the whole attempt, `_to_thread` hops included, so this
                # cannot interleave with `_ensure_mqtt` / `_sync_home_seq` or the retry.
                async with self._mqtt_lock:
                    if self._mqtt is None:
                        await self._start_mqtt_locked()
                        ok = self._mqtt is not None
                    else:
                        await self._api.login_if_stale(self._api.auth_gen)
                        await self._to_thread(self._mqtt.close)
                        await self._to_thread(self._mqtt.connect_blocking)
                        ok = True
            except Exception as exc:
                self.log(f"navien mqtt: reconnect failed: {exc}")
            if ok:
                # `connect_blocking` returns before CONNACK, so this is as far as the loop
                # can see. If the link never actually comes up, `_on_disconnect` arms it
                # again — and the walk is only reset by a connection that *held*
                # (`_after_connect`, once a frame has arrived), so a flapping link keeps
                # walking outward. Resetting on `_on_connect` did not do that: a flap
                # reaches `_on_connect` every time.
                return

    async def _after_connect(self) -> None:
        """Re-ask the mat for its shadow on every (re)connect, then watch for the answer.

        Same reasoning as the AirOne's: `connect_blocking` returns before `subscribe()` has
        run and the client id is regenerated per connection, so a request fired at the
        return of `connect_blocking` can be answered into a subscription that does not
        exist yet. Dispatching from `_on_connect` removes the race instead of hiding it.
        """
        seen = self._reports
        await self._request_initial()
        await asyncio.sleep(INITIAL_STATE_TIMEOUT_S)
        if self._closing:
            return
        if self._reports == seen:
            self.log(f"navien mqtt: no state within {INITIAL_STATE_TIMEOUT_S}s of connect")
            return
        # B5. The reconnect walk is reset here, not on the connect callback: a frame has
        # arrived and the link is still up, which is the only available evidence that the
        # connection *held* rather than flapped. Resetting on `_on_connect` meant a flap —
        # which reaches it every time — walked back to MQTT_BACKOFF_S[0] on each cycle, one
        # full two-step login every few seconds on a one-session account.
        if self._mqtt is not None and self._mqtt.connected:
            self._backoff_index = 0

    def _schedule_mqtt_retry(self) -> None:
        """Retry MQTT start-up on the MQTT_BACKOFF_S walk instead of waiting a poll tick.

        Only safe because Phase 1 made `_secured_sign_in` raise on an empty body: before
        that, a login could report success with `aws` still None, and retrying would have
        spun straight back into that silent half-login.

        One task at a time, and the walk saturates at MQTT_BACKOFF_S[-1] (= the poll
        interval), so the worst case is no worse than the behaviour it replaces.
        """
        if self._closing:
            return
        pending = self._mqtt_retry_task
        # `is not current_task()` is load-bearing. The retry below re-enters `_start_mqtt`,
        # which lands right back here when the credentials still have not arrived — and at
        # that moment `_mqtt_retry_task` is the task doing the asking, so a plain
        # "already scheduled?" check would refuse and the walk would stop after one step.
        # The handle is kept (rather than cleared) so `on_uninit` can still cancel it.
        if (pending is not None and not pending.done()
                and pending is not asyncio.current_task()):
            return
        delay = MQTT_BACKOFF_S[self._mqtt_retry_step]
        self._mqtt_retry_step = min(self._mqtt_retry_step + 1, len(MQTT_BACKOFF_S) - 1)
        self.log(f"navien mqtt: no AWS credentials; retrying in {delay}s")

        async def retry() -> None:
            await asyncio.sleep(delay)
            if self._closing:
                return
            try:
                # Retrying alone can never help: only a fresh secured-sign-in mints AWS
                # credentials. `login_if_stale` keeps that from becoming a login storm when
                # several devices are waiting on the same missing credentials.
                await self._api.login_if_stale(self._api.auth_gen)
            except Exception as exc:
                self.log(f"mqtt retry login failed: {exc}")
                self._schedule_mqtt_retry()
                return
            await self._start_mqtt()

        self._mqtt_retry_task = asyncio.create_task(retry())

    async def _start_mqtt(self) -> None:
        """Build the MQTT client, serialised against every other client mutation (B3).

        The entry point for callers that do not already hold `_mqtt_lock`: `_run` at boot
        and the missing-credentials retry task. `_ensure_mqtt` and `_reconnect_loop` hold it
        already and call `_start_mqtt_locked` — asyncio.Lock is not reentrant.
        """
        async with self._mqtt_lock:
            await self._start_mqtt_locked()

    async def _start_mqtt_locked(self) -> None:
        # Gate 0 Q6. `0/mate/#` is a perfectly *valid* topic filter, so a home_seq of 0
        # connects and subscribes successfully and then no frame ever arrives. Returning
        # without assigning `self._mqtt` is what keeps `_ensure_mqtt` retrying rather than
        # parking on a healthy-looking connection to the wrong tree.
        if not self._home_seq:
            self.log("navien: home_seq is 0 — refusing to start MQTT "
                     "(재로그인 후 앱을 재시작하세요)")
            return
        if not self._api.aws:
            self.log("no AWS credentials; realtime push disabled, polling only")
            self._schedule_mqtt_retry()
            return
        # A restarted poll task re-enters `_run` and lands back here, so an earlier client
        # can still be alive. Dropping the reference without closing it would leave paho's
        # network thread running for a connection nothing reads.
        if self._mqtt is not None:
            await self._to_thread(self._mqtt.close)
            self._mqtt = None
        loop = asyncio.get_running_loop()
        self._mqtt = NavienMqtt(
            loop=loop,
            user_seq=self._api.user_seq,
            home_seq_provider=lambda: self._home_seq,
            creds_provider=lambda: self._api.aws,
            on_reported=self._on_reported,
            on_connected=self._on_mqtt_connected,
            on_disconnected=self._on_mqtt_disconnected,
            prefixes=("mate",),
            parser=extract_mate_reported,
            log=self.log,
        )
        try:
            await self._to_thread(self._mqtt.connect_blocking)
            self._mqtt_retry_step = 0        # a connection resets the backoff walk
        except Exception as exc:
            self.log(f"mqtt connect failed (falling back to polling): {exc}")
            self._mqtt = None

    async def _request_initial(self) -> None:
        """Nudge the mat to publish its current shadow state (empty desired)."""
        if self._closing:
            return
        try:
            await self._mate({})
        except Exception as exc:
            self.log(f"initial-state request failed: {exc}")

    # --- push --------------------------------------------------------------

    def _on_reported(self, device_id: str, reported: dict) -> None:
        # B6. Unlike the connect/disconnect callbacks, reports do not go through
        # `NavienMqtt._dispatch` and its `_closing` check — `_on_message` marshals them
        # with a bare `call_soon_threadsafe`. A frame landing during `_teardown`'s gather
        # would otherwise spawn an `_apply_state` outside the snapshot teardown cancels,
        # writing capabilities on a dismantled Device.
        if self._closing:
            return
        if self._mat is None:
            return
        if device_id and device_id != self._device_id:
            # I4: the topic's last segment is used when the payload omits the device id,
            # which on a `.../res` topic yields the literal "res" — a real frame silently
            # dropped here, indistinguishable from no frame at all.
            self.log(f"navien mqtt: unmatched frame device_id={device_id!r} matched=False "
                     f"(expected {self._device_id!r})")
            return
        self._reports += 1
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
