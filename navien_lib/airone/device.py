"""One paired Navien AirOne unit.

Logs into the cloud and subscribes to the realtime MQTT push, which is the *only* source
of live state — there is no REST fallback for it, and this docstring used to claim one.
`GET /devices` is a capability document: upstream navien_smart_ha builds its device from
that exact payload and deliberately fills in no reported state at all, and says plainly
that power, running state and errors come from the status response. So REST supplies the
air-quality readings MQTT does not carry, the capability metadata (humidity range,
whether a sensor is attached) and the cloud's own `connected` flag — never a live
running/mode/airVolume value. Air-quality is REST-only.

Control is REST: a capability change posts a command and the device's own reply, pushed
back over MQTT, is what updates the capability — so the UI reflects the appliance, not
an optimistic guess.

State is deep-merged, never replaced, because reports arrive partial. See docs/PORTING.md.
"""

import asyncio
import random
import time

from homey import device

from navien_lib import compat, i18n
from navien_lib.const import (
    AIRONE_CMD_CHANGE_MODE,
    AIRONE_CMD_POWER,
    AIRONE_CMD_STATUS,
    AIRONE_READBACK_DELAY_S,
    CODE_BAD_REQUEST,
    FAN_ADJUSTABLE_MODES,
    HUMIDITY_STEP,
    INITIAL_STATE_TIMEOUT_S,
    MODES_WITH_HUMIDITY,
    MQTT_BACKOFF_S,
    OPTION_SAVER,
    OPTION_SLEEP,
    OPTION_TURBO,
    POLL_INTERVAL_S,
    POLL_JITTER,
    POLL_START_JITTER,
    SETTING_HOME_SEQ,
    STALE_AFTER_S,
    STORE_DEVICE_ID,
    STORE_DEVICE_SEQ,
    STORE_MODEL_CODE,
    STORE_PHYSICAL_ID,
)
from navien_lib.navien.airone import AironeDevice, air_sensor_changes
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
# report arriving in that gap still carries the *old* value and would snap a just-changed
# control back. After a command we show the requested value immediately (optimistic) and
# hold it — ignoring control updates — until either the device confirms the change (a
# report matching what we asked for, released early) or this window elapses. Kept
# generous because a confirming report ends it early anyway.
_SETTLE_S = 5.0

# "no value has been seen yet", which `None` cannot express here: None is a real state of
# the cloud's `connected` flag (the key was absent from the payload).
_UNSET = object()


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
        # Gate 0 Q6: a home_seq of 0 still makes a *valid* MQTT topic filter (`0/airone/#`),
        # so the connection succeeds and no frame ever arrives. Log what each device
        # actually resolved at boot before deciding whether the guard is needed.
        self.log(f"navien: home_seq={self._home_seq} for {self.get_name()}")
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
        # B3. Three independent tasks mutate `self._mqtt` with `await` points in between:
        # the poll loop (`_ensure_mqtt`, `_sync_home_seq`), the event-driven reconnect, and
        # the missing-credentials retry. Nothing serialised them, and `_ensure_mqtt`'s
        # `if self._mqtt.connected: return` early-out is inert precisely while a reconnect
        # is in flight — so two of them could interleave close/close/connect/connect on the
        # same object and orphan a fully connected client that still shares `_connected`
        # and `_on_disconnect` with the live one, i.e. self-sustaining churn at one full
        # `login()` per cycle. Worse, the retry walk saturates at MQTT_BACKOFF_S[-1], which
        # *equals* POLL_INTERVAL_S, so the retry task and the poll tick are designed to
        # converge on the same period. Every close/connect_blocking pair — including the
        # `_to_thread` hops — runs under this lock.
        self._mqtt_lock = asyncio.Lock()
        self._tasks: set = set()
        self._poll_task = None
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
        # The last `connected` flag this device logged, so the line is printed on every
        # *change* including the first read. It used to be a one-shot per boot, which is
        # the reason the 1 -> 0 transition during the hardware test left no trace at all —
        # the flag had already been spent on the first cycle's 1. A transition is the only
        # thing worth a line here; repeating an unchanged value 288 times a day is not.
        self._logged_connected = _UNSET
        # Event-driven reconnect: the task walking MQTT_BACKOFF_S while the push link is
        # down, and the step it is on. A connection resets the step (see _on_mqtt_connected).
        self._reconnect_task = None
        self._backoff_index = 0
        # When a link event last wrote `_connected_registry`, so a `GET /devices` response
        # that was already in flight at that moment cannot overwrite it. The body of such a
        # response is a snapshot from *before* the event, so applying it would undo a newer
        # fact with an older one — and for a `/disconnected` that means the tile goes back
        # to available for a full cycle, which is the exact wait this feature removes.
        self._event_at = None
        # The three availability/freshness signals. They answer three different questions
        # and no two of them are substitutes:
        #   _connected_registry — device <-> cloud, the cloud's own statement (authority)
        #   _rest_failures      — cloud <-> us over REST (the link that carries control)
        #   _last_report_at     — cloud <-> us over MQTT (the only source of live state)
        self._connected_registry = None
        self._last_report_at = None
        # Consecutive poll cycles whose device-list read failed, and the REST arm of the
        # matrix in full. Availability is driven by this rather than by "did _poll_once
        # throw": F2 gave every sub-call its own guard, so the method throwing stopped
        # being a signal about the link at all. It is a count and not a timestamp on
        # purpose — the matrix is specified as "two consecutive cycles", and an age
        # threshold would have to be re-derived from the jittered poll interval and could
        # then disagree with the count at the boundary. One fact, one signal.
        self._rest_failures = 0
        # Staleness is measured from the last report, or from boot for a device that has
        # never reported at all — which is precisely the state the marker exists for.
        self._started_at = time.monotonic()

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
        # value (see _SETTLE_S), and the control values we're waiting for the device to
        # confirm so the hold can end early.
        self._settle_until = 0.0
        self._pending: dict = {}

        self.log(f"{self.get_name()} init (seq={self._device_seq}, phys={self._physical_id})")
        self._poll_task = asyncio.create_task(self._run())
        self._poll_task.add_done_callback(self._on_poll_task_done)

    async def on_uninit(self) -> None:
        await self._teardown()

    async def _teardown(self) -> None:
        """Cancel every task this device owns, *wait* for them, and only then close MQTT.

        The await is the whole point for the asyncio half (M5). `.cancel()` returns
        immediately, so without it `close()` runs while `_run` may still be sitting inside
        `_poll_once`; if that `_run` reaches `_ensure_mqtt` before the cancellation lands,
        it finds `_mqtt` freshly emptied and builds a brand-new client — the device
        resurrects its own MQTT connection in the middle of being dismantled.

        What the gather does *not* do is stop an executor thread, and the only blocking
        calls those tasks make are `_to_thread(close)` / `_to_thread(connect_blocking)`.
        Cancelling one of those raises in the coroutine at once while the pool worker runs
        on, so this can return from `gather` with a `connect_blocking` still in flight.
        Two things close that: `_mqtt_lock`, taken below so no cancelled-but-still-running
        *coroutine* can be mid-sequence, and NavienMqtt's own refusal to install (or its
        unwinding of) a client while `_closing` is set — which is what actually covers the
        thread. Neither alone is enough.

        `_tasks` is swept here too (readbacks, reapplies, the connect watchdog): before
        this, nothing ever cancelled them.
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
        # Acquired after the cancellations, never before: a task holding it would have to
        # be cancelled to release it, and it cannot be cancelled by a teardown that is
        # itself parked on the lock.
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
        """Log in, start MQTT push, then poll REST forever as the fallback."""
        await self._acquire_api()
        await self._start_mqtt()
        try:
            await self._poll_once(initial=True)
        except Exception as exc:
            self.log(f"initial poll failed: {exc}")
            # Only when the initial poll never reached its own availability verdict. A
            # device must not start life greyed out because the session was still coming up.
            await self._safe_available()
        # The boot attempt is deliberately outside the matrix's two-cycle budget: it is
        # already excused above, so counting it would make the first real cycle the second
        # strike and grey the device out one tick after start-up.
        self._rest_failures = 0

        # One-shot 0-30 s offset (POLL_START_JITTER of the tick) applied to the first loop
        # sleep only, never before the initial poll — de-phasing the three device types
        # must not cost the user half a minute of empty tiles at boot.
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
            # REST and MQTT are independent links, so they get independent guards. Sharing
            # one `try` meant a single failed REST read cancelled that cycle's reconnect
            # check, and the push link stayed down for another full interval for no reason.
            try:
                await self._ensure_mqtt()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"mqtt check failed: {exc}")

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

        Demoted, not deleted. `_on_mqtt_disconnected` now recovers the link in seconds
        instead of up to a poll tick, so this is a cheap once-per-cycle sanity net for the
        cases no event can report — a client that never got built, or one paho believes is
        up while nothing arrives. It is kept deliberately as the rollback target: reverting
        the event-driven path leaves this behind, still working on its own.

        The whole body runs under `_mqtt_lock` (B3). The `connected` early-out below reads
        like a guard against doing this twice, but it is inert in exactly the case that
        matters: while a reconnect is in flight the client is *not* connected, so the check
        waves this cycle straight through into a second close/connect on the same object.
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

    # --- event-driven reconnect (F3) ---------------------------------------

    def _on_mqtt_connected(self) -> None:
        """The push link came up. Already marshalled onto the loop by NavienMqtt.

        Deliberately does *not* reset `_backoff_index` — see `_after_connect`. A flapping
        link reaches this callback on every flap, so resetting here made the walk restart
        at MQTT_BACKOFF_S[0] each time and turned the backoff into a fixed 5 s retry with
        a full two-step `login()` behind it (`login_if_stale` is handed the current
        generation, so a lone device never dedups against anyone).
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

    def _on_mqtt_event(self, device_id: str, connected: bool) -> None:
        """The *appliance's* link to the cloud changed. Already marshalled onto the loop.

        This is the fast signal, not a new authority. The cloud publishes it within about a
        minute of the event — measured on hardware (2026-08-03): power was cut and the
        `/disconnected` frame arrived ~65 s later; power came back and `/connected` landed
        at 11:40:45, while the REST poll that would have restored the tile was not due until
        ~11:45:09. Four and a half minutes of holding the answer and not using it.

        The poll still overwrites `_connected_registry` every cycle from `GET /devices`, so
        an event that was missed, or one that was wrong, is corrected by the next tick at
        the latest. Nothing here removes or weakens that path.
        """
        if self._closing:
            return
        if device_id not in (self._device_id, self._physical_id):
            self.log(f"navien mqtt: link event device_id={device_id!r} matched=False "
                     f"(expected {self._device_id!r} / {self._physical_id!r})")
            return
        self.log(f"navien mqtt: link event connected={connected} for {device_id}")
        self._connected_registry = connected
        self._event_at = time.monotonic()
        self._spawn(self._after_event(connected))

    async def _after_event(self, connected: bool) -> None:
        """Act on the link event now instead of at the next poll — that is its whole value.

        The status re-request on `/connected` is the same reasoning `_after_connect` uses
        after a resubscribe: the appliance has just come back and what it is doing is
        unknown, and it is the appliance's own reply that fills the tile. It goes through
        `_request_status`, so the dead-listener guard (7.8) still applies.
        """
        await self._update_availability()
        # Read live, not from the captured argument: a `/connected` immediately superseded
        # by a `/disconnected` would otherwise POST a status command to an appliance this
        # same method just declared offline. `_update_availability` already reads live for
        # the same reason, so the two now agree on which verdict they are acting on.
        if self._connected_registry:
            await self._request_status()

    async def _reconnect_loop(self) -> None:
        """Walk MQTT_BACKOFF_S until the push link is back, saturating at 300 s.

        F3: paho's own `reconnect_delay_set` retries the *same* presigned path forever, and
        that path carries an STS token with no `X-Amz-Expires` — so once the credentials
        behind it go stale, paho can retry until the heat death of the universe and never
        succeed. Only a fresh login re-mints them, which paho cannot do. Recovery therefore
        has to live out here, and before this it was pinned to the 300 s poll tick.
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
                # cannot interleave with the poll task's `_ensure_mqtt` / `_sync_home_seq`
                # or with the missing-credentials retry.
                async with self._mqtt_lock:
                    if self._mqtt is None:
                        await self._start_mqtt_locked()
                        ok = self._mqtt is not None
                    else:
                        # `login_if_stale` rather than `login`: several devices lose the
                        # link to the same outage and this account invalidates the previous
                        # session on every login, so unconditional logins would bounce each
                        # other.
                        await self._api.login_if_stale(self._api.auth_gen)
                        await self._to_thread(self._mqtt.close)
                        await self._to_thread(self._mqtt.connect_blocking)
                        ok = True
            except Exception as exc:
                self.log(f"navien mqtt: reconnect failed: {exc}")
            if ok:
                # As far as this loop can tell, that is success: `connect_blocking` returns
                # right after `loop_start()` and CONNACK lands later on paho's own thread.
                # If it never lands, `_on_disconnect` arms this loop again — and the walk is
                # only reset by a connection that *held* (`_after_connect`, once a frame has
                # actually arrived), so a flapping link keeps walking outward. Resetting on
                # `_on_connect` did not do that: a flap reaches `_on_connect` every time, so
                # the index went back to 0 on each cycle and the "backoff" was a 5 s loop
                # with a full login in it.
                return

    async def _after_connect(self) -> None:
        """Re-request state on every successful (re)connect, then watch for the answer.

        Not "functionally identical to boot", which is why it exists: `connect_blocking`
        returns straight after `loop_start()`, `subscribe()` happens later inside
        `_on_connect` on paho's thread, and the client id is regenerated per connection
        while the server routes replies by `clientId`. At boot that race is hidden by
        accident — `_poll_once(initial=True)` makes two REST round trips before it reaches
        the status request. A reconnect has no such delay, so the request is dispatched from
        `_on_connect` itself, after subscribe and after `connected` is True.

        Deliberately NOT the rest of `_poll_once`: the model-code refresh, the humidity
        range sync and the trailing `_apply_state()` all ride the poll tick, which is at
        most POLL_INTERVAL_S away, and none of them changes on a reconnect.
        """
        mark = self._last_report_at
        await self._request_status()
        # F14: INITIAL_STATE_TIMEOUT_S was a dead constant. It only becomes a real watchdog
        # now that *every* connection is followed by a request — before this, silence after
        # a reconnect was normal, so a timeout would have been pure false alarm.
        await asyncio.sleep(INITIAL_STATE_TIMEOUT_S)
        if self._closing:
            return
        if self._last_report_at == mark:
            self.log(f"navien mqtt: no state within {INITIAL_STATE_TIMEOUT_S}s of connect")
            return
        # B5. This, and not `_on_connect`, is where the reconnect walk earns its reset: the
        # link has carried a frame and is still up, which is the only evidence available
        # that the connection *held* rather than flapped. Resetting on the connect callback
        # instead meant a flapping link — which does reach `_on_connect`, every flap —
        # walked back to MQTT_BACKOFF_S[0] each time, i.e. one full two-step login every
        # few seconds on an account that allows a single session.
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
        and the missing-credentials retry task. `_ensure_mqtt` and `_reconnect_loop` hold
        it already and call `_start_mqtt_locked` directly — asyncio.Lock is not reentrant,
        so going through here would deadlock them.
        """
        async with self._mqtt_lock:
            await self._start_mqtt_locked()

    async def _start_mqtt_locked(self) -> None:
        # Gate 0 Q6. `0/airone/#` is a perfectly *valid* topic filter, so a home_seq of 0
        # connects and subscribes successfully and then no frame ever arrives — a healthy
        # looking connection to the wrong tree. Returning without assigning `self._mqtt`
        # is what keeps `_ensure_mqtt` retrying instead of parking on it.
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
        # One MQTT client per device is a decision, not an omission awaiting cleanup, and
        # it is gated on exactly one condition: build an app-level MQTT/session hub only
        # when a *second device of the same type* is added. Until then the topic prefixes
        # differ per device, so there are no duplicate frames for a hub to deduplicate, and
        # the one real benefit a hub would bring — single ownership of `login()` — is
        # already held by `login_if_stale(gen)`, which buys it without putting a new shared
        # component on the realtime path. Nothing here is built in anticipation of that hub.
        self._mqtt = NavienMqtt(
            loop=loop,
            user_seq=self._api.user_seq,
            home_seq_provider=lambda: self._home_seq,
            creds_provider=lambda: self._api.aws,
            on_reported=self._on_reported,
            on_connected=self._on_mqtt_connected,
            on_disconnected=self._on_mqtt_disconnected,
            on_event=self._on_mqtt_event,
            log=self.log,
        )
        try:
            await self._to_thread(self._mqtt.connect_blocking)
            self._mqtt_retry_step = 0        # a connection resets the backoff walk
        except Exception as exc:
            self.log(f"mqtt connect failed (falling back to polling): {exc}")
            self._mqtt = None

    # --- polling / initial state ------------------------------------------

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
        # reconnect attempt running at the same moment would otherwise close and rebuild
        # the object this is trying to discard.
        async with self._mqtt_lock:
            if self._mqtt is not None:
                await self._to_thread(self._mqtt.close)
                self._mqtt = None

    async def _poll_once(self, initial: bool = False) -> None:
        """Re-read device state and air-quality over REST.

        Also nudges the appliance to publish a fresh MQTT report by sending a `status`
        command — shadow state only arrives on change otherwise, so a just-added device
        would sit empty until first touched.
        """
        await self._sync_home_seq()
        wants_sensors = True
        # The device list was the one unguarded call left in this method (the air-sensor
        # read, the status request and the humidity-range sync all guard themselves), so
        # it alone could abort the rest of the cycle.
        rest_ok = False
        # Stamped before the request goes out, so the reply can be compared against any
        # link event that arrived while it was in flight.
        started = time.monotonic()
        try:
            devices = await self._api.list_devices(self._home_seq)
            rest_ok = True
        except Exception as exc:
            self.log(f"device list read failed: {exc}")
            devices = []
        # A response that was already in flight when an event landed describes the world
        # from before it; applying it would undo a newer fact with an older one. Two guards,
        # for two different non-statements: a read that *failed* says nothing about the
        # appliance, and a read that *predates the event* says nothing about now. Without
        # the second one a `/disconnected` is silently reverted and the tile reads available
        # for another full cycle — the exact wait acting on the event was meant to remove.
        superseded = self._event_at is not None and self._event_at > started
        if superseded:
            self.log("navien: device-list reading predates a link event; keeping the event")
        apply_registry = rest_ok and not superseded
        if apply_registry:
            # A device that is not in this cycle's list is *unknown*, not offline: carrying
            # last cycle's True forward would let a device vanish from the account and keep
            # reading as connected.
            self._connected_registry = None
        for raw in devices:
            unit = AironeDevice.from_raw(raw, log=self.log)
            if unit and str(unit.device_id) == self._device_id:
                # Gate 0 Q4: the cloud publishes a per-device online flag that this port
                # used to throw away (the whole list is already fetched and walked, so
                # reading one more field costs nothing). Logged on every change — one entry
                # per device rather than per list, because one remembered value cannot be
                # compared against several devices' flags.
                flag = raw.get("connected")
                if flag != self._logged_connected:
                    self._logged_connected = flag
                    self.log(f"navien: device-list connected={flag!r} "
                             f"({type(flag).__name__}) for {raw.get('deviceId')!r}")
                # The one field in this response that is live rather than a capability
                # descriptor, and the reason the device list is still read every cycle.
                # Skipped when a link event overtook this reply — see `superseded` above.
                if apply_registry:
                    self._connected_registry = unit.connected_registry
                # Refresh the model code (control-topic addressing) from the live list,
                # so a device paired before the model-code fix corrects itself.
                if unit.model_code:
                    self._model_code = unit.model_code
                # Never `self._unit.apply_reported(unit.reported)`. That is settled from
                # upstream rather than inferred from the payloads we happen to have seen:
                # navien_smart_ha's `AironeDevice.parse` reads this very response and
                # deliberately sets no `reported` — the field is a default_factory dict
                # that only MQTT ever fills — and the source states outright that power,
                # running state and errors come from the *status* response. Its coordinator
                # then re-attaches the MQTT state (`device.reported = old.reported`) on
                # every device-list refresh, i.e. it treats this response as the thing that
                # must not win. Merging it anyway is upstream issue #12: `roomController.
                # mode` here is the supported-combinations array, not the live int, so the
                # mode blanks out and the fan picker goes unavailable.
                #
                # Three things are read off this transient unit and none of them is live
                # state — the humidity range and `wants_air_sensors` are capability
                # descriptors, and `connected` (above) is the cloud's link flag. Fenced by
                # test_poll_once_never_merges_device_list_into_live_state and
                # test_humidity_range_comes_from_the_transient_unit.
                await self._sync_humidity_range(unit.humidity_range())
                wants_sensors = unit.wants_air_sensors()
                break

        if wants_sensors:
            try:
                sensors = await self._api.air_sensor(self._device_seq, self._home_seq)
                previous = dict(self._unit.air_sensors)
                self._unit.apply_air_sensors(sensors)
                # Deliberate instrumentation, and the thing it is instrumenting is not this
                # device: `/air-sensor` is served by the cloud against the *AirOne's*
                # device_seq, and the AirMonitor reads the same endpoint. Whether the
                # monitor reports independently or through its parent is open (see
                # airmonitor/device.py), and it is settled by comparing what these two
                # lines say during an AirOne outage. Changed values only, so a working
                # link is one short line and a frozen one is visibly "unchanged".
                changed = air_sensor_changes(previous, self._unit.air_sensors)
                self.log(f"navien: air-sensor {changed or 'unchanged'}")
            except Exception as exc:
                self.log(f"air-sensor read failed: {exc}")

        # The probe. This was `if self._unit.is_on or initial:` — a *power* gate, which is
        # a mis-port of upstream's docstring (PORTING.md:81); upstream's code gates on
        # connectivity (coordinator.py:570-572). The power form is a permanent trap: the
        # only other caller is the post-command readback. The plan replaced it with a
        # connectivity gate (`initial or self._connected_registry is not False`) on the
        # theory that a unit Homey believes is off could never be asked again, so an
        # external power-on would go unseen until someone commanded it from Homey.
        #
        # Measured on real hardware (Gate 0 Q5, 2026-08-03) and the theory does not hold:
        # the appliance publishes state changes over MQTT unprompted. With the unit off
        # and no REST request made since the previous tick, powering it on from the phone
        # app updated the tile before the next poll. So the only hole the power gate ever
        # left is the reconnect gap — a change that happens while we are not subscribed —
        # and `on_connected` closes that directly by re-requesting state after every
        # subscribe. The connectivity gate bought a second cover for a hole that already
        # has one, at 288 status POSTs a day per powered-off-but-online unit against an
        # undocumented API. Reverted; `connected_registry` still drives availability.
        #
        # Boot note: the guard inside `_request_status` also tests `self._mqtt.connected`,
        # so this call can be suppressed at boot when the CONNACK has not landed yet. That
        # is covered — better — by `on_connected`, which fires after subscribe with the
        # link known good. Anyone tracing the boot path will see the probe apparently
        # disappear; it moved.
        if self._unit.is_on or initial:
            await self._request_status()

        # Reset on an explicit success, never inferred from "_poll_once did not throw":
        # F2 gave every sub-call in this method its own guard, so the absence of an
        # exception stopped saying anything about the REST link. That inference is what
        # would have made this signal unreadable.
        if rest_ok:
            self._rest_failures = 0
        else:
            self._rest_failures += 1
        await self._update_availability()
        await self._apply_state()

    async def _update_availability(self) -> None:
        """The availability matrix (plan §5 Phase 3.3), in priority order.

        Only two arms take the device away from the user, and both are statements of fact
        rather than heuristics. The third failing arm — REST fine, device online, reports
        stale — deliberately stays *available*: control genuinely reaches the appliance,
        so greying the tile out would remove working controls to report a display problem.
        That arm gets the staleness marker in `_apply_state` instead.
        """
        if self._rest_failures >= 2:
            await self._safe_unavailable("나비엔 서버에 연결할 수 없습니다")
            return
        if self._connected_registry is False:
            # The cloud's own statement about its link to the appliance. A control POST
            # will not arrive, so a stale marker here would be a lie by omission.
            await self._safe_unavailable("기기가 오프라인입니다")
            return
        await self._safe_available()

    def _is_stale(self) -> bool:
        """True once the newest MQTT report is older than STALE_AFTER_S.

        Measured from boot for a device that has never reported: that is not an edge case
        to tolerate but the exact state the marker exists for — a tile showing nothing,
        with no indication that nothing is what it means.
        """
        since = self._last_report_at if self._last_report_at is not None else self._started_at
        return (time.monotonic() - since) > STALE_AFTER_S

    async def _request_status(self) -> None:
        # 7.8, and deliberately a superset of "is None or _closing": the literal form does
        # not test the connection, so it leaves the actual defect — POSTing a status
        # request every cycle to a listener that died — completely in place. Safe only
        # because `on_connected` is dispatched from `_on_connect` after `_connected = True`,
        # so the reconnect's own re-request always finds `connected` already True.
        if self._mqtt is None or self._closing or not self._mqtt.connected:
            return
        try:
            await self._airone(AIRONE_CMD_STATUS, desired=None)
        except Exception as exc:
            self.log(f"status request failed: {exc}")

    # --- push --------------------------------------------------------------

    def _on_reported(self, device_id: str, reported: dict) -> None:
        """MQTT callback (already marshalled onto the loop)."""
        # B6, and the only one of the three MQTT callbacks that needed saying twice:
        # `_on_mqtt_connected`/`_on_mqtt_disconnected` are dispatched through
        # `NavienMqtt._dispatch`, which drops them once the socket is closing, but reports
        # take a bare `call_soon_threadsafe` in `_on_message` that bypasses it entirely.
        # A frame landing during `_teardown`'s gather would therefore spawn an
        # `_apply_state` task *outside* the snapshot teardown cancels, and it would write
        # capabilities on a dismantled Device.
        if self._closing:
            return
        if device_id and device_id not in (self._device_id, self._physical_id):
            # I4. `extract_airone_reported` falls back to the topic's last segment when
            # roomController carries no deviceId, which on a `.../res` topic yields the
            # literal "res" — a real frame that then drops out of this filter in complete
            # silence, indistinguishable from the push link being dead.
            self.log(f"navien mqtt: unmatched frame device_id={device_id!r} matched=False "
                     f"(expected {self._device_id!r} / {self._physical_id!r})")
            return
        # The freshness clock. Only meaningful because the probe above is now unconditional
        # for an online device: "older than two probes" reads as failure, not as idleness.
        self._last_report_at = time.monotonic()
        self._unit.apply_reported(reported)
        # If this report confirms the change we're holding for, end the hold now so the
        # confirmed state shows immediately instead of waiting out the window.
        if self._settle_until and self._pending_confirmed():
            self._settle_until = 0.0
            self._pending = {}
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

    def _pending_from_desired(self, desired) -> dict:
        """The control values a command asks for, so a matching report can end the hold."""
        rc = (desired or {}).get("roomController") or {}
        pending: dict = {}
        for key in ("running", "mode", "option", "airVolume"):
            if key in rc:
                try:
                    pending[key] = int(rc[key])
                except (TypeError, ValueError):
                    pass
        extra = rc.get("additionalData")
        if isinstance(extra, dict) and "value" in extra:
            try:
                pending["humidity"] = int(extra["value"])
            except (TypeError, ValueError):
                pass
        return pending

    def _pending_confirmed(self) -> bool:
        """True once the model reflects everything the pending command asked for."""
        if not self._pending:
            return False
        u = self._unit
        now = {"running": u.running, "mode": u.mode, "option": u.option,
               "airVolume": u.air_volume, "humidity": u.target_humidity}
        return all(now.get(k) == v for k, v in self._pending.items())

    async def _optimistic(self, desired) -> None:
        """Reflect a just-sent command right away and hold it briefly.

        The appliance needs ~3 s to accept the command and report back; until then its
        reports still carry the old value. We merge the requested state into the model,
        push it now, and set a settle deadline so lagging reports can't snap the control
        back — but a report that *confirms* the change ends the hold early (see
        _on_reported). A readback nudges a fresh report.
        """
        self._unit.apply_reported(desired)
        self._pending = self._pending_from_desired(desired)
        self._settle_until = time.monotonic() + _SETTLE_S
        self._schedule_readback()
        await self._apply_state(force=True)

        async def reapply():
            # After the window, push the confirmed (or, if the command was rejected,
            # reverted) state so a failed command doesn't leave a stale optimistic value.
            await asyncio.sleep(_SETTLE_S + 0.5)
            if time.monotonic() >= self._settle_until:   # not extended by a newer command
                self._settle_until = 0.0
                self._pending = {}
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
        # Retry transient failures. A single blip (session bounce, network) used to make
        # the capability listener reject, which reverts the tile's Quick Action toggle —
        # so a "power on" would visibly not take. Commands are idempotent (turning an
        # already-on unit on is a no-op), so re-sending is safe.
        last = None
        for attempt in range(3):
            try:
                client_id = (self._mqtt.client_id if self._mqtt
                             else f"rest-U{self._api.user_seq}")
                return await self._api.airone_command(
                    device_seq=self._device_seq,
                    home_seq=self._home_seq,
                    model_code=self._model_code,
                    physical_device_id=self._physical_id,
                    client_id=client_id,
                    command=command,
                    desired=desired,
                )
            except Exception as exc:
                last = exc
                self.log(f"airone {command} failed (attempt {attempt + 1}/3): {exc}")
                # F10. The retry above exists for *transient* failures, and a 400 is the
                # server's verdict on the command itself — the same body will be rejected
                # the same way, so the remaining attempts buy nothing and cost the user
                # 3.6 s of a frozen tile (0.6 + 1.2 + 1.8) plus two more control POSTs at
                # an appliance that already said no. `code` is None on every
                # NavienNetworkError, which is what keeps the retryable case retryable:
                # a request that never reached the server carries no verdict to obey.
                if getattr(exc, "code", None) == CODE_BAD_REQUEST:
                    self.log(f"airone {command} rejected (400); not retrying")
                    break
                await asyncio.sleep(0.6 * (attempt + 1))
        raise last

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
        # The staleness marker rides the existing free-text status capability, so it needs
        # no new capability and no manifest change, and it sits outside the settle gate
        # below so an optimistic hold can never suppress it.
        #
        # The non-None floor is mandatory, not defensive: `status_text()` returns None on
        # an empty model and `_set` returns early on None, so without it a device that
        # connected and never received a frame would show no status line *and* no marker —
        # the one state the marker is most needed in.
        text = u.status_text(self._language)
        if self._is_stale():
            text = (f"{text} · {i18n.translate('stale', self._language)}" if text
                    else i18n.translate("stale_alone", self._language))
        await self._set("navien_airone_status", text)
        await self._set("navien_auto_dry_percent", self._num(u.auto_dry_percent))
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
