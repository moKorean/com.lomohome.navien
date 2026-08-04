"""Device start-up / poll-loop tests, run against the fake `homey` in conftest.py.

These are the app's first tests of the device layer itself. They drive real asyncio tasks:
pytest-asyncio is not a dev dependency and this suite does not add one, so each test owns
its loop via `asyncio.run` and the poll interval is monkeypatched down to milliseconds.

What they are fencing, in one sentence each: a single network error at boot must not kill
a poll task forever (M1), a died task must restart while a torn-down one must not (the
done-callback), a failed REST read must not cancel that cycle's MQTT reconnect check (F2),
and the device list — a capability document, not live state — must never be merged into
the MQTT-reported state (the P5/§4.4 fence).

Phase 3 adds the recovery and honesty half: a deliberate `close()` must not re-trigger the
reconnect it is dismantling, a failed connect must not leave the suppression flag stuck,
every (re)connect must re-ask for state, a dead push link must stop being POSTed to, and
the three availability signals — the cloud's own `connected` flag, the REST link, and the
age of the newest report — must each produce the answer they alone can give.
"""

import asyncio

import pytest
from homey import device as conftest_device  # the fake installed by tests/conftest.py

from navien_lib.airmonitor import device as airmonitor_device
from navien_lib.airone import device as airone_device
from navien_lib.const import (
    AIRONE_CMD_POWER,
    CODE_BAD_REQUEST,
    HUMIDITY_TYPE,
    SETTING_HOME_SEQ,
    STALE_AFTER_S,
    STORE_DEVICE_ID,
    STORE_DEVICE_SEQ,
    STORE_MODEL_CODE,
    STORE_MONITOR_ID,
    STORE_PHYSICAL_ID,
    STORE_ZONE_ID,
)
from navien_lib.mate import device as mate_device
from navien_lib.navien.api import AwsCredentials, NavienApiError, NavienNetworkError
from navien_lib.navien.mqtt import NavienMqtt

TICK = 0.01          # stands in for POLL_INTERVAL_S = 300.0


class FakeApi:
    """The shared NavienApi as the device modules use it: `aws`, `user_seq`, and the four
    coroutines they call. `fail[name]` makes that call raise, which is how a boot-time
    network error is staged."""

    def __init__(self, *, devices=None, sensors=None, aws=None):
        self.aws = aws
        self.user_seq = "77"
        self.devices = list(devices or [])
        self.sensors = list(sensors or [])
        self.fail: dict = {}
        self.calls: dict = {}

    def _tick(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        exc = self.fail.get(name)
        if exc is not None:
            raise exc

    async def list_devices(self, home_seq):
        self._tick("list_devices")
        return self.devices

    async def air_sensor(self, device_seq, home_seq):
        self._tick("air_sensor")
        return self.sensors

    async def airone_command(self, **kwargs):
        self._tick("airone_command")
        return {}

    async def mate_control(self, **kwargs):
        self._tick("mate_control")
        return {}

    async def login(self):
        self._tick("login")

    @property
    def auth_gen(self) -> int:
        return 0

    async def login_if_stale(self, gen):
        self._tick("login_if_stale")


async def until(predicate, what, timeout=2.0):
    """Wait for a condition the poll loop is supposed to reach, or say what was missing."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def stop(dev):
    """Tear the device's poll task down the way `on_uninit` does, and wait for it."""
    dev._closing = True
    task = dev._poll_task
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


_ABSENT = object()   # "the firmware did not send this key at all", ≠ False


def _airone_raw(*, running=2, air_volume=4, option=3, humidity_min=30, humidity_max=80,
                connected=True):
    """One `GET /devices` entry — a DID *capability document*, not live state.

    `roomController.mode` is the supported-combinations array and the power/fan fields are
    whatever the catalog carries, so the values here are deliberately different from the
    live state the tests seed over MQTT. The one genuinely live field is `connected`, and
    `connected=_ABSENT` omits it entirely — the third state the tri-state flag exists for.
    """
    entry = {
        "serviceCode": 300, "deviceSeq": 12345, "deviceId": "AIR-XYZ",
        "Properties": {
            "nickName": "거실 에어원", "modelCode": 1024,
            "data": {"did": {"reported": {"roomController": {
                "deviceId": "RC-77", "zoneId": 1,
                "mode": [
                    {"name": 9, "additionalData": [
                        {"type": HUMIDITY_TYPE, "min": humidity_min, "max": humidity_max}]},
                    {"name": 10},
                ],
                "running": running, "airVolume": air_volume, "option": option,
                "additionalData": [{"type": HUMIDITY_TYPE,
                                    "min": humidity_min, "max": humidity_max}],
            }}}},
        },
    }
    if connected is not _ABSENT:
        entry["connected"] = connected
    return entry


_AIRONE_STORE = {STORE_DEVICE_SEQ: 12345, STORE_DEVICE_ID: "AIR-XYZ",
                 STORE_PHYSICAL_ID: "RC-77", STORE_MODEL_CODE: 1024}
_MATE_STORE = {STORE_DEVICE_SEQ: 11, STORE_DEVICE_ID: "MAT-1", STORE_MODEL_CODE: 700}
_MONITOR_STORE = {STORE_DEVICE_SEQ: 12345, STORE_MONITOR_ID: "AM-1", STORE_ZONE_ID: 2}


async def _airone_at_rest(make_homey, api, capabilities):
    """An AirOne past `on_init` with its poll loop parked, so a test can drive one call.

    `on_init` starts the loop as its last statement; cancelling before it has taken a step
    means it never runs, so `_api` is assigned here the way `_run` would have.

    A non-zero home_seq is seeded because `_start_mqtt` now refuses to build a client
    without one (Gate 0 Q6): `0/airone/#` is a *valid* filter, so a zero would connect,
    subscribe and then receive nothing forever.
    """
    dev = airone_device.AironeDevice_(
        homey=make_homey(api=api, settings={SETTING_HOME_SEQ: "5"}),
        store=_AIRONE_STORE, capabilities=capabilities, name="거실 에어원")
    await dev.on_init()
    await stop(dev)
    # `stop` is the teardown helper, so it leaves `_closing` True — but a device with its
    # loop parked is not a device being dismantled, and several Phase 3 guards short-circuit
    # on `_closing`. Leaving it set would make those tests pass for the wrong reason.
    dev._closing = False
    dev._api = api
    return dev


# --- M1: a network error at boot must not kill the poll task -----------------


@pytest.mark.parametrize("awaitable", [False, True], ids=["sync", "awaitable"])
def test_mate_survives_boot_time_network_error(make_homey, monkeypatch, awaitable):
    """One URLError at start-up used to end the mat's polling until the app restarted.

    Parametrised over compat.resolve's two contracts (compat.py:20-24): with
    `awaitable=True` the fake homey returns coroutines from settings/i18n/app, which is the
    half that fails silently when the app gets it wrong (compat.py:1-9).
    """
    monkeypatch.setattr(mate_device, "POLL_INTERVAL_S", TICK)

    async def scenario():
        api = FakeApi()
        api.fail["list_devices"] = NavienNetworkError("no route to host")
        dev = mate_device.MateDevice_(
            homey=make_homey(api=api, awaitable=awaitable), store=_MATE_STORE,
            capabilities=["onoff", "navien_operation_mode"], name="안방 매트")
        await dev.on_init()

        await until(lambda: api.calls.get("list_devices", 0) >= 2,
                    "the poll loop to reach a second cycle after the boot failure")
        assert not dev._poll_task.done()
        assert dev.available is True            # _safe_available still ran
        assert any("initial poll failed" in line for line in dev.logs)
        await stop(dev)

    asyncio.run(scenario())


@pytest.mark.parametrize("failing", ["_refresh_model", "_start_mqtt",
                                     "_request_initial", "_apply_state"])
def test_mate_survives_failure_in_each_preloop_call(make_homey, monkeypatch, failing):
    """Guarding only `_refresh_model` would leave the other three as live escape routes,
    so the guard is asserted per pre-loop call, not once."""
    monkeypatch.setattr(mate_device, "POLL_INTERVAL_S", TICK)

    async def scenario():
        api = FakeApi()
        dev = mate_device.MateDevice_(
            homey=make_homey(api=api), store=_MATE_STORE,
            capabilities=["onoff"], name="안방 매트")

        async def boom(*args, **kwargs):
            raise NavienNetworkError(f"network error in {failing}")

        monkeypatch.setattr(dev, failing, boom)
        await dev.on_init()

        await until(lambda: dev.available is True,
                    f"_run to get past {failing} and reach its loop")
        await asyncio.sleep(TICK * 3)
        assert not dev._poll_task.done()
        assert any("initial poll failed" in line for line in dev.logs)
        await stop(dev)

    asyncio.run(scenario())


def test_airmonitor_survives_boot_time_network_error(make_homey, monkeypatch):
    """Same failure, other read-only device: `air_sensor` is the AirMonitor's only call."""
    monkeypatch.setattr(airmonitor_device, "POLL_INTERVAL_S", TICK)

    async def scenario():
        api = FakeApi()
        api.fail["air_sensor"] = NavienNetworkError("no route to host")
        dev = airmonitor_device.AirMonitorDevice_(
            homey=make_homey(api=api), store=_MONITOR_STORE,
            capabilities=["measure_pm25"], name="거실 에어모니터")
        await dev.on_init()

        await until(lambda: api.calls.get("air_sensor", 0) >= 2,
                    "the poll loop to reach a second cycle after the boot failure")
        assert not dev._poll_task.done()
        assert dev.available is True
        assert any("initial poll failed" in line for line in dev.logs)
        await stop(dev)

    asyncio.run(scenario())


# --- the poll task's done-callback ------------------------------------------


def test_done_callback_does_not_restart_on_cancel(make_homey, monkeypatch):
    """`on_uninit` cancels without awaiting, so a dismantled task reaches the callback the
    same way a dead one does — only `task.cancelled()` separates them. Here `_closing` is
    deliberately left False so it is that check, and nothing else, being tested."""
    monkeypatch.setattr(airmonitor_device, "POLL_INTERVAL_S", TICK)

    async def scenario():
        api = FakeApi()
        dev = airmonitor_device.AirMonitorDevice_(
            homey=make_homey(api=api), store=_MONITOR_STORE,
            capabilities=["measure_pm25"], name="거실 에어모니터")
        await dev.on_init()
        await until(lambda: api.calls.get("air_sensor", 0) >= 1, "the first poll")

        original = dev._poll_task
        original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        await asyncio.sleep(TICK * 2)

        assert dev._poll_task is original          # no restart was scheduled
        assert not any("poll task died" in line for line in dev.logs)

        # Invoked directly as well, because the assertions above also hold on a build with
        # no done-callback at all — which is not what this test is fencing.
        dev._on_poll_task_done(original)
        assert dev._poll_task is original

    asyncio.run(scenario())


def test_done_callback_reassigns_poll_task(make_homey, monkeypatch):
    """The restart must land in `_poll_task`. Otherwise a later `on_uninit` cancels the
    dead original, the restarted loop outlives the device, and it goes on writing
    capabilities on a torn-down Device."""
    monkeypatch.setattr(airmonitor_device, "POLL_INTERVAL_S", TICK)

    async def scenario():
        api = FakeApi()
        dev = airmonitor_device.AirMonitorDevice_(
            homey=make_homey(api=api), store=_MONITOR_STORE,
            capabilities=["measure_pm25"], name="거실 에어모니터")
        await dev.on_init()
        await stop(dev)

        # A task that dies on its own, standing in for a poll loop that raised.
        dev._closing = False
        dev._restart_delay = TICK

        async def dies():
            raise RuntimeError("link down")

        died = asyncio.create_task(dies())
        dev._poll_task = died
        died.add_done_callback(dev._on_poll_task_done)

        await until(lambda: dev._poll_task is not died,
                    "the done-callback to reassign _poll_task to the restart")
        assert any("poll task died" in line for line in dev.logs)
        assert not dev._poll_task.done()
        await stop(dev)

    asyncio.run(scenario())


# --- F2: REST and MQTT are independent links --------------------------------


def test_ensure_mqtt_runs_when_poll_raises(make_homey, monkeypatch):
    """A failed REST read used to cancel that cycle's reconnect check as collateral, so
    the push link stayed down for another full interval for no reason."""
    monkeypatch.setattr(airone_device, "POLL_INTERVAL_S", TICK)

    async def scenario():
        api = FakeApi()
        dev = airone_device.AironeDevice_(
            homey=make_homey(api=api), store=_AIRONE_STORE,
            capabilities=["onoff"], name="거실 에어원")

        checks = []

        async def failing_poll(initial=False):
            raise NavienNetworkError("REST down")

        async def record_ensure():
            checks.append(True)

        monkeypatch.setattr(dev, "_poll_once", failing_poll)
        monkeypatch.setattr(dev, "_ensure_mqtt", record_ensure)
        await dev.on_init()

        await until(lambda: len(checks) >= 2,
                    "the MQTT reconnect check to run on a cycle whose REST read failed")
        assert any("poll failed" in line for line in dev.logs)
        await stop(dev)

    asyncio.run(scenario())


# --- the device list is a capability document, not live state ---------------


def test_poll_once_never_merges_device_list_into_live_state(make_homey):
    """`GET /devices` carries no realtime running/mode/option/airVolume — merging it back
    would let the capability document overwrite what MQTT reported (upstream issue #12,
    where the fan picker went unavailable). MQTT is the only source of live AirOne state."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw(running=2, air_volume=4, option=3)])
        dev = await _airone_at_rest(make_homey, api, ["onoff", "navien_target_humidity"])
        dev._unit.apply_reported({"roomController": {
            "running": 1, "mode": 9, "option": 1, "airVolume": 2,
            "additionalData": [{"type": 3, "value": 55}]}})

        await dev._poll_once()

        u = dev._unit
        assert (u.running, u.mode, u.option, u.air_volume) == (1, 9, 1, 2)
        assert u.target_humidity == 55
        assert api.calls.get("list_devices") == 1     # it *was* read, just not merged

    asyncio.run(scenario())


def test_humidity_range_comes_from_the_transient_unit(make_homey):
    """The only thing taken from the device list is the humidity *range*, and it is read
    off the transient unit built from that response. `self._unit` cannot supply it — the
    live model's `roomController.mode` is an int, so its own humidity_range() is always the
    40/70 fallback, which would silently rewrite the slider on every unit whose server
    range differs."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw(humidity_min=30, humidity_max=80)])
        dev = await _airone_at_rest(make_homey, api, ["onoff", "navien_target_humidity"])
        dev._unit.apply_reported({"roomController": {"running": 1, "mode": 9}})

        await dev._poll_once()

        options = dev.capability_options["navien_target_humidity"]
        assert (options["min"], options["max"]) == (30, 80)
        assert dev._unit.humidity_range() == (40, 70)    # not where the range came from

    asyncio.run(scenario())


# --- F10: a verdict the server has already given is not worth re-asking ------


def test_rejected_command_is_not_retried(make_homey):
    """A 400 is the server's ruling on the command body, and the retry loop exists for
    *transient* failures. Re-sending the identical payload twice more only freezes the tile
    while it sleeps 0.6 + 1.2 + 1.8 s and pushes two more control POSTs at an appliance that
    already said no."""

    async def scenario():
        api = FakeApi()
        api.fail["airone_command"] = NavienApiError(
            "POST /devices/9/control -> code 400: bad request", CODE_BAD_REQUEST)
        dev = await _airone_at_rest(make_homey, api, ["onoff"])

        started = asyncio.get_running_loop().time()
        with pytest.raises(NavienApiError):
            await dev._airone(AIRONE_CMD_POWER, desired={"roomController": {"running": 1}})

        assert api.calls["airone_command"] == 1
        assert asyncio.get_running_loop().time() - started < 0.5   # none of the 3.6 s slept
        assert any("not retrying" in line for line in dev.logs)

    asyncio.run(scenario())


def test_network_failure_still_uses_every_retry(make_homey, monkeypatch):
    """The fence on the branch above. NavienNetworkError subclasses NavienApiError, so a
    non-retryable branch written even slightly wider would swallow the one failure the
    retry is most for. It carries no `code`, which is what keeps it out."""

    async def scenario():
        slept = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay, *args, **kwargs):
            slept.append(delay)
            return await real_sleep(0, *args, **kwargs)

        api = FakeApi()
        api.fail["airone_command"] = NavienNetworkError("no route to host")
        dev = await _airone_at_rest(make_homey, api, ["onoff"])

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        with pytest.raises(NavienNetworkError):
            await dev._airone(AIRONE_CMD_POWER, desired=None)
        monkeypatch.undo()

        assert api.calls["airone_command"] == 3
        # Rounded because 0.6 * 3 is 1.7999999999999998 in binary floating point.
        assert [round(d, 2) for d in slept] == [0.6, 1.2, 1.8]   # the 3.6 s a 400 skips

    asyncio.run(scenario())


# --- the home a device is subscribed to can change under it ------------------


def test_home_seq_change_resubscribes(make_homey, monkeypatch):
    """`save_credentials` rewrites SETTING_HOME_SEQ, but a running device read it once in
    `on_init` and the topic filter is chosen inside `_on_connect`. So re-reading the setting
    is only half of it: an already-connected client stays on the old home's tree and no
    frame ever arrives again. The client has to be rebuilt, and the new one has to pick the
    filter up from the provider rather than a value captured at construction."""
    connected, closed = [], []

    def fake_connect(self):
        self._connected = True
        connected.append(self)

    def fake_close(self):
        self._connected = False
        closed.append(self)

    monkeypatch.setattr(NavienMqtt, "connect_blocking", fake_connect)
    monkeypatch.setattr(NavienMqtt, "close", fake_close)

    async def scenario():
        api = FakeApi(aws=AwsCredentials("key", "secret", "token"))
        homey = make_homey(api=api, settings={SETTING_HOME_SEQ: "5"})
        dev = airone_device.AironeDevice_(
            homey=homey, store=_AIRONE_STORE, capabilities=["onoff"], name="거실 에어원")
        await dev.on_init()
        await stop(dev)
        dev._api = api

        await dev._start_mqtt()
        first = dev._mqtt
        assert first._topics() == ["5/airone/#"]

        homey.settings.values[SETTING_HOME_SEQ] = "9"
        await dev._poll_once()          # re-reads the setting and drops the stale client
        await dev._ensure_mqtt()        # …which is what forces the reconnect

        assert closed == [first]
        assert dev._mqtt is not first
        assert dev._mqtt._topics() == ["9/airone/#"]
        assert dev._home_seq == 9       # REST calls follow the new home too
        assert any("home_seq changed 5 -> 9" in line for line in dev.logs)

    asyncio.run(scenario())


# --- M3: a session with no AWS credentials must not cost a whole poll tick ---


def test_missing_aws_credentials_retry_walks_the_backoff(make_homey, monkeypatch):
    """`_start_mqtt` used to just return, so realtime push stayed off until the next poll
    tick noticed — up to POLL_INTERVAL_S. Only a fresh secured-sign-in mints AWS
    credentials, so the retry has to re-login, and it has to keep walking: the second step
    is scheduled from inside the first one's own task, which is the case a naive
    "is a retry already pending?" guard silently swallows."""
    monkeypatch.setattr(airone_device, "MQTT_BACKOFF_S", (0.005, 0.01, 0.02))

    async def scenario():
        api = FakeApi(aws=None)          # a session that logged in without AWS credentials
        dev = await _airone_at_rest(make_homey, api, ["onoff"])
        dev._closing = False             # `_airone_at_rest` parks the loop by tearing down
        dev._mqtt_retry_step = 0

        await dev._start_mqtt()
        await until(lambda: api.calls.get("login_if_stale", 0) >= 3,
                    "the retry to take a third step of the backoff walk")

        delays = [line for line in dev.logs if "retrying in" in line]
        assert "retrying in 0.005s" in delays[0]
        assert "retrying in 0.01s" in delays[1]        # …and it is walking, not repeating
        assert dev._mqtt is None                       # still no push, honestly reported
        dev._closing = True
        dev._mqtt_retry_task.cancel()

    asyncio.run(scenario())


# === Phase 3 ================================================================
#
# 3.1 — the socket-level `_closing` flag and F13


class _SyncDisconnectClient:
    """paho's shape as `NavienMqtt.close()` drives it.

    `close()` calls `loop_stop()` and then `disconnect()`. Once `loop_stop()` has joined
    paho's network thread, `_packet_queue` writes on the *calling* thread, so the DISCONNECT
    goes out inline and `on_disconnect` fires synchronously inside `close()`. That is the
    whole reason the `_closing` flag has to exist: without it the teardown announces a drop
    to the very reconnect path it is dismantling.
    """

    def __init__(self, owner):
        self._owner = owner
        self.stopped = False

    def loop_stop(self):
        self.stopped = True

    def disconnect(self):
        self._owner._on_disconnect(self, None)


def _mqtt(loop, **kwargs):
    """A NavienMqtt with the plumbing every test here needs and nothing more."""
    kwargs.setdefault("on_reported", lambda *_a: None)
    return NavienMqtt(loop=loop, user_seq="77", home_seq_provider=lambda: 5,
                      creds_provider=lambda: AwsCredentials("key", "secret", "token"),
                      log=lambda *_a: None, **kwargs)


def test_close_fires_no_reconnect():
    """A deliberate `close()` must dispatch zero disconnect callbacks. The drop it causes
    is not a drop, and treating it as one makes the reconnect re-trigger itself out of its
    own teardown — the hot loop in risk row 2."""

    async def scenario():
        events = []
        m = _mqtt(asyncio.get_running_loop(), on_disconnected=lambda: events.append("down"))
        m._client = _SyncDisconnectClient(m)
        m._connected = True

        m.close()
        await asyncio.sleep(0)          # let any call_soon_threadsafe callback run

        assert events == []
        assert m._closing is True       # still suppressing until a connect clears it

    asyncio.run(scenario())


def test_closing_flag_cleared_when_connect_raises():
    """`connect_blocking` raises in exactly the case the reconnect exists for. Clearing the
    flag anywhere but a `finally` leaves it stuck True and suppresses the disconnect
    callback for the rest of the app's life — the same class of permanently-dead recovery
    that M1 fixed, reintroduced by its own fix."""

    async def scenario():
        events = []
        m = _mqtt(asyncio.get_running_loop(), on_disconnected=lambda: events.append("down"))
        m._client = _SyncDisconnectClient(m)
        m.close()
        assert m._closing is True

        def unreachable():
            raise NavienNetworkError("no route to the IoT endpoint")

        m._creds_provider = unreachable
        with pytest.raises(NavienNetworkError):
            m.connect_blocking()

        assert m._closing is False
        # …and the callback genuinely works again, which is the thing being fenced.
        m._on_disconnect(None, None)
        await asyncio.sleep(0)
        assert events == ["down"]

    asyncio.run(scenario())


def test_close_on_dead_socket_clears_connected():
    """F13. `close()` used to null `_client` and leave `_connected` True, and a True with no
    client makes `_ensure_mqtt` return early forever — realtime push off for good. A socket
    already gone makes `loop_stop()` raise, which is the path that has to be checked."""

    class Dead:
        def loop_stop(self):
            raise OSError("socket already gone")

        def disconnect(self):
            raise OSError("socket already gone")

    async def scenario():
        m = _mqtt(asyncio.get_running_loop())
        m._client = Dead()
        m._connected = True

        m.close()

        assert m.connected is False
        assert m._client is None

    asyncio.run(scenario())


class FakeMqtt:
    """The device layer's view of NavienMqtt: `connected`, `client_id`, `close()`."""

    def __init__(self, connected=True):
        self.connected = connected
        self.client_id = "cid-77"
        self.closed = 0
        self.connects = 0

    def close(self):
        self.closed += 1
        self.connected = False

    def connect_blocking(self):
        self.connects += 1
        self.connected = True


def test_reconnect_walks_backoff_and_a_bare_connect_does_not_reset_it(
        make_homey, monkeypatch):
    """The reconnect walks MQTT_BACKOFF_S rather than retrying at a fixed rate. paho's own
    retry cannot do this job at all: it replays the one presigned path it was handed, and
    that path carries an STS token nothing inside paho can re-mint.

    This used to assert that `_on_mqtt_connected` resets the walk, which is the defect and
    not the contract (B5): a flapping link *does* reach `_on_connect`, on every flap, so the
    index went back to MQTT_BACKOFF_S[0] each cycle and the backoff degenerated into a 5 s
    retry loop with a full two-step `login()` inside it — on an account that allows one
    session. What earns the reset now is a connection that held; see the test below.
    """
    monkeypatch.setattr(airone_device, "MQTT_BACKOFF_S", (0.005, 0.01, 0.02))
    monkeypatch.setattr(airone_device, "INITIAL_STATE_TIMEOUT_S", 60.0)
    failures = {"left": 0}

    def fake_connect(self):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise NavienNetworkError("iot endpoint unreachable")
        self._connected = True

    monkeypatch.setattr(NavienMqtt, "connect_blocking", fake_connect)
    monkeypatch.setattr(NavienMqtt, "close", lambda self: None)

    async def scenario():
        api = FakeApi(aws=AwsCredentials("key", "secret", "token"))
        dev = await _airone_at_rest(make_homey, api, ["onoff"])
        dev._closing = False
        await dev._start_mqtt()
        assert dev._mqtt is not None

        failures["left"] = 2                 # two attempts fail, the third gets through
        dev._backoff_index = 0
        dev._on_mqtt_disconnected()
        await until(lambda: dev._reconnect_task.done(), "the reconnect loop to get back in")

        walked = [line for line in dev.logs if "reconnect attempt" in line]
        assert "reconnect attempt 1 in 0.005s" in walked[0]
        assert "reconnect attempt 2 in 0.01s" in walked[1]      # walking, not repeating
        assert "reconnect attempt 3 in 0.02s" in walked[2]
        assert dev._backoff_index == 2                          # saturated at the last step

        dev._on_mqtt_connected()             # a connect on its own proves nothing
        await asyncio.sleep(0)
        assert dev._backoff_index == 2
        await dev._teardown()

    asyncio.run(scenario())


def test_backoff_resets_only_after_a_connection_that_held(make_homey, monkeypatch):
    """B5. The walk is reset by evidence that the link *worked*, not that it came up.

    `_on_mqtt_connected` fires on every flap, and each reset put the next reconnect back at
    MQTT_BACKOFF_S[0] with `login_if_stale(self._api.auth_gen)` behind it — which, read at
    the call site by a lone device, always performs a full two-step login. The two halves
    below are one contract: a connection that carries no frame leaves the walk where it was,
    and one that carries a frame and is still up puts it back to the first step.
    """
    monkeypatch.setattr(airone_device, "INITIAL_STATE_TIMEOUT_S", 0.01)

    async def scenario():
        api = FakeApi()
        dev = await _airone_at_rest(make_homey, api, ["onoff"])
        dev._mqtt = FakeMqtt(connected=True)

        dev._backoff_index = 3
        dev._on_mqtt_connected()
        await until(lambda: not dev._tasks, "the connect watchdog to finish with no report")
        assert dev._backoff_index == 3
        assert any("no state within" in line for line in dev.logs)

        dev._on_mqtt_connected()
        await until(lambda: api.calls.get("airone_command", 0) >= 2,
                    "the second connect to re-ask for state")
        dev._on_reported("AIR-XYZ", {"roomController": {"running": 1, "mode": 9}})
        await until(lambda: dev._backoff_index == 0,
                    "the walk to reset once the link had actually carried a frame")
        await dev._teardown()

    asyncio.run(scenario())


# 3.2 — re-request state on every (re)connect


def test_on_connected_fires_status_request_after_subscribe(make_homey, monkeypatch):
    """Two halves of one contract. In NavienMqtt the callback is dispatched after
    `subscribe()` and after `connected` is True — load-bearing, because the device's own
    request guard tests `connected` and would otherwise cancel the request it was woken to
    make. In the device, the callback turns into an actual status POST."""
    monkeypatch.setattr(airone_device, "INITIAL_STATE_TIMEOUT_S", 60.0)

    async def scenario():
        order = []

        class Client:
            def subscribe(self, topic, qos=0):
                order.append(("subscribe", topic))

        m = _mqtt(asyncio.get_running_loop())
        m._on_connected = lambda: order.append(("callback", m.connected))
        m._on_connect(Client(), None, None, object())     # reason_code without is_failure
        await asyncio.sleep(0)
        assert order == [("subscribe", "5/airone/#"), ("callback", True)]

        api = FakeApi(aws=AwsCredentials("key", "secret", "token"))
        dev = await _airone_at_rest(make_homey, api, ["onoff"])
        dev._closing = False
        dev._mqtt = FakeMqtt(connected=True)
        dev._backoff_index = 3

        dev._on_mqtt_connected()
        await until(lambda: api.calls.get("airone_command", 0) >= 1,
                    "the reconnect to re-ask the appliance for its state")
        # Untouched here on purpose (B5): coming up is not evidence the link will hold, and
        # the reset moved to `_after_connect`, behind an actual frame.
        assert dev._backoff_index == 3
        await dev._teardown()

    asyncio.run(scenario())


def test_request_status_suppressed_when_link_dead(make_homey):
    """7.8. The draft guard ("is None or _closing") does not test the connection, so it
    does not fix 7.8 at all — a dead listener keeps being POSTed to every cycle. The guard
    has to be a superset: no client, closing, *or* not connected."""

    async def scenario():
        api = FakeApi()
        dev = await _airone_at_rest(make_homey, api, ["onoff"])
        dev._mqtt = FakeMqtt(connected=False)

        await dev._request_status()

        assert api.calls.get("airone_command") is None

    asyncio.run(scenario())


def test_request_status_suppressed_while_closing(make_homey):
    """A device being torn down must not send one last command on its way out."""

    async def scenario():
        api = FakeApi()
        dev = await _airone_at_rest(make_homey, api, ["onoff"])
        dev._mqtt = FakeMqtt(connected=True)
        dev._closing = True

        await dev._request_status()

        assert api.calls.get("airone_command") is None

    asyncio.run(scenario())


# 3.3 — availability and freshness


_STATUS_CAPS = ["onoff", "navien_airone_status"]


def test_stale_marker_appears_and_clears(make_homey):
    """The marker rides the existing free-text status capability, so it needs no new
    capability and no manifest change, and it is applied outside the settle gate so an
    optimistic hold can never suppress it."""

    async def scenario():
        api = FakeApi()
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._on_reported("AIR-XYZ", {"roomController": {"running": 1, "mode": 9,
                                                        "airVolume": 3, "option": 1}})
        await dev._apply_state()
        fresh = dev.get_capability_value("navien_airone_status")
        assert "최신 아님" not in fresh

        dev._last_report_at -= STALE_AFTER_S + 1
        await dev._apply_state()
        assert dev.get_capability_value("navien_airone_status") == f"{fresh} · 최신 아님"

        # A report clears it, and the tile goes back to exactly what it said before.
        dev._on_reported("AIR-XYZ", {"roomController": {"running": 1, "mode": 9,
                                                        "airVolume": 3, "option": 1}})
        await dev._apply_state()
        assert dev.get_capability_value("navien_airone_status") == fresh

    asyncio.run(scenario())


def test_marker_renders_on_never_reported_device(make_homey):
    """C5. `status_text()` returns None on an empty model and `_set` returns early on None,
    so without the non-None floor a device that connected and never received a frame shows
    no status line *and* no marker — the one state the marker is most needed in."""

    async def scenario():
        api = FakeApi()
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        assert dev._unit.status_text("ko") is None      # nothing for a marker to hang off
        dev._started_at -= STALE_AFTER_S + 1

        await dev._apply_state()

        assert dev.get_capability_value("navien_airone_status") == "상태 정보 없음"

    asyncio.run(scenario())


def test_status_capability_untouched_when_neither_state_nor_staleness(make_homey):
    """The floor must not become a value of its own: a device that has simply not reported
    *yet* is not stale, and writing anything here would put a permanent placeholder on
    every freshly paired tile."""

    async def scenario():
        api = FakeApi()
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)

        await dev._apply_state()

        assert dev.get_capability_value("navien_airone_status") is None

    asyncio.run(scenario())


def test_rest_failure_twice_marks_unavailable(make_homey):
    """One failed cycle is a blip; two consecutive ones are the link. Homey greys the tile
    out and blocks Flow actions, which is the honest answer when the REST link that carries
    every control POST is down."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw()])
        dev = await _airone_at_rest(make_homey, api, ["onoff"])
        api.fail["list_devices"] = NavienNetworkError("no route to host")

        await dev._poll_once()
        assert dev.available is True                 # one failure is not a verdict

        await dev._poll_once()
        assert dev.available is False
        assert dev.unavailable_reason == "나비엔 서버에 연결할 수 없습니다"

    asyncio.run(scenario())


def test_guarding_rest_substeps_does_not_suppress_unavailable(make_homey):
    """The PV2 fence. F2 gave every sub-call in `_poll_once` its own guard, so the method
    stopped raising — and any availability signal derived from "did the poll throw" would
    have been silently switched off by that. The signal is set on an explicit success
    instead, so it survives the guards that surround it."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw()])
        dev = await _airone_at_rest(make_homey, api, ["onoff"])
        api.fail["list_devices"] = NavienNetworkError("no route to host")
        api.fail["air_sensor"] = NavienNetworkError("no route to host")

        await dev._poll_once()
        await dev._poll_once()          # neither call raises — that is the point

        assert any("device list read failed" in line for line in dev.logs)
        assert any("air-sensor read failed" in line for line in dev.logs)
        assert dev.available is False
        assert dev._rest_failures == 2       # both cycles counted, neither one swallowed

    asyncio.run(scenario())


def test_connected_false_marks_unavailable_not_stale(make_homey):
    """The arm the two-signal model could not read. "REST fine but reports stale" merges
    *the appliance fell off the network* with *our subscription broke*, and those need
    opposite answers from the user. The cloud's own flag separates them: an explicit False
    means a control POST will not arrive, so a stale marker there would be a lie."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw(connected=False)])
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._mqtt = FakeMqtt(connected=True)

        await dev._poll_once()

        assert dev._connected_registry is False
        assert dev.available is False
        assert dev.unavailable_reason == "기기가 오프라인입니다"
        # …and an offline device is not probed: that is the gate upstream actually uses.
        assert api.calls.get("airone_command") is None

    asyncio.run(scenario())


def test_connected_absent_is_treated_as_unknown(make_homey):
    """A deliberate divergence from upstream, which stores `bool(raw.get("connected"))` and
    so fails *closed*: a firmware that omits the key would read as permanently offline. We
    keep None and test `is not False`, so an absent field costs nothing."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw(connected=_ABSENT)])
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._mqtt = FakeMqtt(connected=True)

        await dev._poll_once()

        assert dev._connected_registry is None
        assert dev.available is True                     # unknown, not assumed dead

    asyncio.run(scenario())


def test_powered_off_unit_is_not_probed_every_cycle(make_homey):
    """C8, decided by measurement rather than by argument.

    The plan replaced the power gate with a connectivity gate, on the theory that a unit
    Homey believes is off would never be asked for status again and so could never be
    seen to turn on. Gate 0 Q5 on real hardware (2026-08-03) refuted the premise: the
    appliance publishes state changes over MQTT unprompted, so an external power-on
    arrives without any probe. The only hole the power gate leaves is the reconnect gap,
    which `on_connected` closes directly.

    This fences the revert: a powered-off unit costs no status POST per cycle. Removing
    the `is_on` term again would be a 288-POST-a-day regression against an undocumented
    API whose observed failure mode is session contention."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw()])
        dev = await _airone_at_rest(make_homey, api, ["onoff"])
        dev._mqtt = FakeMqtt(connected=True)
        dev._unit.apply_reported({"roomController": {"running": 2}})   # 정지
        assert dev._unit.is_on is False

        await dev._poll_once()                                          # not `initial`

        assert api.calls.get("airone_command", 0) == 0

        # …and the moment it is running again, the probe returns — the readback is not
        # the only thing keeping a live unit's state fresh.
        dev._unit.apply_reported({"roomController": {"running": 1}})    # 운전
        await dev._poll_once()
        assert api.calls.get("airone_command") == 1

    asyncio.run(scenario())


# 3.1 — teardown (M5)


def test_teardown_cancels_and_awaits_all_tasks(make_homey, monkeypatch):
    """M5/N3. `.cancel()` returns immediately, so without the gather `close()` runs while
    `_run` may still be inside `_poll_once`; a `_run` that reaches `_ensure_mqtt` first
    rebuilds the client that was just torn down and the device resurrects its own MQTT
    connection mid-teardown. The assertion is that every task is *done* by the time
    teardown returns, not merely that cancel was called."""
    monkeypatch.setattr(airone_device, "POLL_INTERVAL_S", TICK)

    async def scenario():
        api = FakeApi()
        dev = airone_device.AironeDevice_(
            homey=make_homey(api=api, settings={SETTING_HOME_SEQ: "5"}),
            store=_AIRONE_STORE, capabilities=["onoff"], name="거실 에어원")
        await dev.on_init()
        dev._mqtt = FakeMqtt()

        async def forever():
            await asyncio.sleep(3600)

        dev._spawn(forever())                        # a readback/reapply still in flight
        dev._reconnect_task = asyncio.create_task(forever())
        dev._mqtt_retry_task = asyncio.create_task(forever())
        tracked = list(dev._tasks) + [dev._reconnect_task, dev._mqtt_retry_task,
                                      dev._poll_task]

        await dev._teardown()

        assert all(task.done() for task in tracked)
        assert dev._mqtt.closed == 1

    asyncio.run(scenario())


# Gate 0 Q6 — a home_seq of 0 makes a valid filter, which is exactly the problem


def test_home_seq_zero_refuses_mqtt(make_homey):
    """`0/airone/#` is a *valid* topic filter, so the connection succeeds, the subscription
    succeeds, and no frame ever arrives — a healthy-looking client on the wrong tree.
    Refusing without assigning `self._mqtt` is what keeps `_ensure_mqtt` retrying rather
    than parking on it."""

    async def scenario():
        api = FakeApi(aws=AwsCredentials("key", "secret", "token"))
        dev = airone_device.AironeDevice_(
            homey=make_homey(api=api), store=_AIRONE_STORE,
            capabilities=["onoff"], name="거실 에어원")          # no home_seq setting
        await dev.on_init()
        await stop(dev)
        dev._api = api
        assert dev._home_seq == 0

        await dev._start_mqtt()

        assert dev._mqtt is None
        assert any("refusing to start MQTT" in line for line in dev.logs)
        # The retry path stays open: `_ensure_mqtt` sees None and tries again next cycle.
        await dev._ensure_mqtt()
        assert dev._mqtt is None
        assert len([line for line in dev.logs if "refusing to start MQTT" in line]) == 2

    asyncio.run(scenario())


# The AirMonitor answers P3 the other way round


def test_airmonitor_goes_unavailable_after_two_failed_polls(make_homey, monkeypatch):
    """REST is this monitor's only link and `_poll_once` merges rather than replaces, so a
    permanent failure leaves yesterday's PM2.5 on screen with nothing to say so. There is
    no control to lose by admitting it and no free-text capability to carry a marker."""
    monkeypatch.setattr(airmonitor_device, "POLL_INTERVAL_S", TICK)

    async def scenario():
        api = FakeApi()
        api.fail["air_sensor"] = NavienNetworkError("no route to host")
        dev = airmonitor_device.AirMonitorDevice_(
            homey=make_homey(api=api), store=_MONITOR_STORE,
            capabilities=["measure_pm25"], name="거실 에어모니터")
        await dev.on_init()

        # Boot + two loop cycles: the boot attempt is deliberately outside the budget, so
        # a device does not start life greyed out because the session was still coming up.
        await until(lambda: api.calls.get("air_sensor", 0) >= 3, "two failed poll cycles")
        await until(lambda: dev.available is False, "the monitor to admit it has no data")
        assert dev.unavailable_reason == "공기질 데이터를 가져올 수 없습니다"

        api.fail.pop("air_sensor")
        await until(lambda: dev.available is True, "recovery to bring the monitor back")
        await dev._teardown()

    asyncio.run(scenario())


# B3 — nothing serialised MQTT (re)start, so two paho clients could exist at once
#
# Driven deterministically rather than by timing: a fake `_to_thread` parks whatever is
# inside `connect_blocking` on an asyncio.Event, which is exactly the window the real code
# spends in an executor thread. What the second task does *while* the first is parked is
# the whole finding.


def _gate_to_thread(dev, events, gate):
    """Replace `_to_thread` with one that logs the call and parks connects on `gate`."""

    async def fake_to_thread(fn, *args):
        name = fn.__name__
        owner = getattr(fn, "__self__", None)
        events.append((f"{name}:start", owner))
        if name == "connect_blocking":
            await gate.wait()
        result = fn(*args)
        events.append((f"{name}:done", owner))
        return result

    dev._to_thread = fake_to_thread


def test_second_ensure_mqtt_cannot_interleave_with_one_in_flight(make_homey):
    """B3, shape A. `_ensure_mqtt`'s `if self._mqtt.connected: return` early-out is inert
    precisely when it would matter: a reconnect in flight has already closed the socket, so
    `connected` is False and the next caller is waved straight through into a second
    close/connect on the same object. Two of them interleaving orphans a fully connected
    client that still shares `_connected` and `_on_disconnect` with the live one — a
    self-sustaining churn costing one full `login()` per cycle on a one-session account."""

    async def scenario():
        api = FakeApi(aws=AwsCredentials("key", "secret", "token"))
        dev = await _airone_at_rest(make_homey, api, ["onoff"])
        client = FakeMqtt(connected=False)
        dev._mqtt = client
        events, gate = [], asyncio.Event()
        _gate_to_thread(dev, events, gate)

        first = asyncio.create_task(dev._ensure_mqtt())
        await until(lambda: ("connect_blocking:start", client) in events,
                    "the first reconnect to reach connect_blocking")
        second = asyncio.create_task(dev._ensure_mqtt())
        await asyncio.sleep(0.02)          # ample: everything here is ready-to-run

        # The finding, in one assertion: while a reconnect is mid-flight the second caller
        # has touched neither the client nor the session.
        assert events == [("close:start", client), ("close:done", client),
                          ("connect_blocking:start", client)]
        assert api.calls["login_if_stale"] == 1

        gate.set()
        await asyncio.gather(first, second)

        assert client.closed == 1 and client.connects == 1
        assert api.calls["login_if_stale"] == 1     # the second found it connected and left
        await dev._teardown()

    asyncio.run(scenario())


def test_concurrent_start_mqtt_never_leaves_two_clients(make_homey, monkeypatch):
    """B3, shape B. With `_mqtt` None the retry task and the poll tick both enter
    `_start_mqtt` — and they are *designed* to converge, because the retry walk saturates
    at MQTT_BACKOFF_S[-1] (300) which is exactly POLL_INTERVAL_S. Unserialised, the second
    closes the client the first is still connecting and then builds its own, so the first
    `connect_blocking` completes on a client nothing holds: paho's network thread survives
    with no owner. Serialised, the second finds the finished client and replaces it
    properly."""
    events, gate = [], asyncio.Event()

    def connect_blocking(self):
        self._connected = True

    def close(self):
        self._connected = False

    monkeypatch.setattr(NavienMqtt, "connect_blocking", connect_blocking)
    monkeypatch.setattr(NavienMqtt, "close", close)

    async def scenario():
        api = FakeApi(aws=AwsCredentials("key", "secret", "token"))
        dev = await _airone_at_rest(make_homey, api, ["onoff"])
        _gate_to_thread(dev, events, gate)

        first = asyncio.create_task(dev._start_mqtt())
        await until(lambda: events, "the first start to reach connect_blocking")
        one = dev._mqtt
        second = asyncio.create_task(dev._start_mqtt())
        await asyncio.sleep(0.02)

        assert events == [("connect_blocking:start", one)]   # the second is still waiting
        assert dev._mqtt is one

        gate.set()
        await asyncio.gather(first, second)
        two = dev._mqtt

        assert two is not one
        assert events == [
            ("connect_blocking:start", one), ("connect_blocking:done", one),
            ("close:start", one), ("close:done", one),          # closed, never orphaned
            ("connect_blocking:start", two), ("connect_blocking:done", two),
        ]
        await dev._teardown()

    asyncio.run(scenario())


def test_mate_paired_without_a_power_switch_gains_one(make_homey):
    """A mat paired while `powerCtrl` gated `onoff` keeps a tile with no power control.

    Capabilities are fixed at pairing (`mate/driver.py`), so dropping the gate is only half
    the fix — without this the affected owner would have to delete and re-add the device.
    The listener matters as much as the capability: added after the registration loop, the
    switch would appear and then do nothing when pressed.
    """

    async def scenario():
        api = FakeApi()
        dev = mate_device.MateDevice_(
            homey=make_homey(api=api), store=_MATE_STORE,
            capabilities=["navien_operation_mode"], name="안방 매트")
        await dev.on_init()

        assert dev.added_capabilities == ["onoff"]
        assert "onoff" in dev.get_capabilities()
        assert "onoff" in dev.listeners
        assert any("added the onoff capability" in line for line in dev.logs)
        await stop(dev)

    asyncio.run(scenario())


def test_mate_power_migration_says_so_when_the_runtime_cannot_do_it(make_homey, monkeypatch):
    """`add_capability` is not in the Python runtime's documented Device API.

    If it turns out not to exist, the owner needs to hear that re-pairing is the way out —
    a migration that fails silently reads exactly like one that worked.
    """
    # Model a runtime without it. Via monkeypatch so the method comes back afterwards:
    # deleting it from the shared fake class outright would leak into every later test.
    # What is left is `_Strict.__getattr__` raising AttributeError, which is exactly what
    # the probe's `getattr` default absorbs.
    monkeypatch.delattr(conftest_device.Device, "add_capability")

    async def scenario():
        api = FakeApi()
        dev = mate_device.MateDevice_(
            homey=make_homey(api=api), store=_MATE_STORE,
            capabilities=["navien_operation_mode"], name="안방 매트")
        await dev.on_init()

        assert "onoff" not in dev.get_capabilities()
        assert any("re-pair the mat" in line for line in dev.logs)
        assert not dev._poll_task.done()          # and it keeps running regardless
        await stop(dev)

    asyncio.run(scenario())


def test_mate_serialises_mqtt_restart_too(make_homey):
    """The same lock in the other device module. `mate/device.py` carries a line-for-line
    copy of the three racing paths, so a fix applied to only one of them leaves the finding
    live for every sleep mat on the account."""

    async def scenario():
        api = FakeApi(aws=AwsCredentials("key", "secret", "token"))
        dev = mate_device.MateDevice_(
            homey=make_homey(api=api, settings={SETTING_HOME_SEQ: "5"}),
            store=_MATE_STORE, capabilities=["onoff"], name="안방 매트")
        await dev.on_init()
        await stop(dev)
        dev._closing = False
        dev._api = api
        client = FakeMqtt(connected=False)
        dev._mqtt = client
        events, gate = [], asyncio.Event()
        _gate_to_thread(dev, events, gate)

        first = asyncio.create_task(dev._ensure_mqtt())
        await until(lambda: ("connect_blocking:start", client) in events,
                    "the first reconnect to reach connect_blocking")
        second = asyncio.create_task(dev._ensure_mqtt())
        await asyncio.sleep(0.02)

        assert events == [("close:start", client), ("close:done", client),
                          ("connect_blocking:start", client)]
        assert api.calls["login_if_stale"] == 1

        gate.set()
        await asyncio.gather(first, second)
        assert client.closed == 1 and client.connects == 1
        await dev._teardown()

    asyncio.run(scenario())


# B4 — the gather cannot stop an executor thread, so NavienMqtt has to refuse the client


class _FakePaho:
    """paho's `Client` as `connect_blocking` drives it, with an ordered call log.

    `on_close` lets a test stage the one thing the asyncio lock cannot prevent: a `close()`
    landing on another pool worker while this build is in flight.
    """

    log: list = []

    def __init__(self, *_a, on_connect_hook=None, loop_start_hook=None, **_kw):
        self.on_connect = self.on_message = self.on_disconnect = None
        self._hook = on_connect_hook
        self._loop_start_hook = loop_start_hook

    def subscribe(self, topic, qos=0):
        _FakePaho.log.append(f"subscribe:{topic}")

    def tls_set_context(self, _ctx):
        pass

    def ws_set_options(self, path=None):
        pass

    def reconnect_delay_set(self, min_delay=0, max_delay=0):
        pass

    def connect(self, *_a, **_kw):
        _FakePaho.log.append("connect")
        if self._hook is not None:
            self._hook()

    def loop_start(self):
        _FakePaho.log.append("loop_start")
        if self._loop_start_hook is not None:
            self._loop_start_hook()

    def loop_stop(self):
        _FakePaho.log.append("loop_stop")

    def disconnect(self):
        _FakePaho.log.append("disconnect")


def _install_fake_paho(monkeypatch, *, on_connect_hook=None, loop_start_hook=None):
    import paho.mqtt.client as paho

    _FakePaho.log = []
    monkeypatch.setattr(
        paho, "Client",
        lambda *a, **kw: _FakePaho(*a, on_connect_hook=on_connect_hook,
                                   loop_start_hook=loop_start_hook, **kw))
    return _FakePaho.log


def test_a_connack_inside_connect_blocking_is_not_swallowed(monkeypatch):
    """Where `_closing` is cleared, not just that it is.

    It used to be cleared in a trailing `finally`, which runs *after* `loop_start()` — so a
    CONNACK read by paho's thread in that gap was dispatched into a still-suppressing
    `_dispatch` and the `on_connected` callback was dropped. The link would then be up with
    nothing re-requesting state and nothing resetting the reconnect walk, which is
    indistinguishable from the link never coming up. Clearing it at the top of the method
    covers the raising path just as well — nothing below sets it again on its own — and
    leaves the whole body running with an honest flag, which is also what gives the two
    mid-build guards above a meaning they could not otherwise have.
    """
    holder = {}

    def connack():
        holder["m"]._on_connect(_FakePaho(), None, None, object())

    _install_fake_paho(monkeypatch, loop_start_hook=connack)

    async def scenario():
        events = []
        m = _mqtt(asyncio.get_running_loop(), on_connected=lambda: events.append("up"))
        holder["m"] = m
        m._client = _SyncDisconnectClient(m)
        m.close()                       # leaves `_closing` set, as it must
        assert m._closing is True

        m.connect_blocking()
        await asyncio.sleep(0)          # let the marshalled callback run

        assert events == ["up"]
        assert m.connected is True

    asyncio.run(scenario())


def test_connect_refuses_to_install_a_client_closed_mid_build(monkeypatch):
    """B4. `_teardown`'s gather returns as soon as the *coroutine* is cancelled, while the
    executor thread under `_to_thread(connect_blocking)` runs on — so teardown's `close()`
    can be queued on a different pool worker mid-build. Installing anyway would
    `loop_start()` a network thread that outlives the device with nothing holding a
    reference to it."""
    log = _install_fake_paho(monkeypatch)

    async def scenario():
        m = _mqtt(asyncio.get_running_loop())
        # The close lands while the client is being built — here, from the point the
        # credentials are read, which is inside the same window.
        m._creds_provider = lambda: (m.close(), AwsCredentials("k", "s", "t"))[1]

        m.connect_blocking()

        assert log == []                # never connected, never started a network thread
        assert m._client is None
        assert m.connected is False

    asyncio.run(scenario())


def test_connect_unwinds_a_client_closed_after_it_was_installed(monkeypatch):
    """The other half of the same guard, and it is not redundant: a `close()` landing
    between the check and `loop_start()` finds `_client` already set, so it calls
    `loop_stop()` on a loop that has not started and the next statement starts it — the
    orphan the check exists to prevent, one statement later. `_closing` stays set (nothing
    clears it but a later `connect_blocking`), so re-reading it after `loop_start()` is
    what catches that ordering."""

    holder = {}

    def close_now():
        holder["m"].close()

    log = _install_fake_paho(monkeypatch, on_connect_hook=close_now)

    async def scenario():
        m = _mqtt(asyncio.get_running_loop())
        holder["m"] = m

        m.connect_blocking()

        # loop_start is unavoidable — close() ran before it — so the contract is that the
        # thread it starts is stopped again rather than left running.
        assert log[:2] == ["connect", "loop_stop"]      # the close()'s own stop
        assert log[2:] == ["disconnect", "loop_start", "loop_stop", "disconnect"]
        assert m._client is None
        assert m.connected is False

    asyncio.run(scenario())


# B6 — reports bypass `_dispatch`, so the device has to hold its own `_closing` guard


def test_report_arriving_during_teardown_is_dropped(make_homey):
    """B6. `_on_mqtt_connected`/`_on_mqtt_disconnected` go through `NavienMqtt._dispatch`
    and its `_closing` check, but `_on_message` marshals reports with a bare
    `call_soon_threadsafe` that bypasses it. A frame landing during `_teardown`'s gather
    therefore spawned an `_apply_state` task *outside* the snapshot teardown cancels, which
    then wrote capabilities on a dismantled Device."""

    async def scenario():
        api = FakeApi()
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._closing = True

        dev._on_reported("AIR-XYZ", {"roomController": {"running": 1, "mode": 9}})

        assert dev._tasks == set()          # nothing spawned past the teardown snapshot
        assert dev._unit.running is None    # and the model was not touched either
        assert dev._last_report_at is None

    asyncio.run(scenario())


def test_mate_report_arriving_during_teardown_is_dropped(make_homey):
    """The same guard in the other device module that owns an MQTT client."""

    async def scenario():
        api = FakeApi(devices=[])
        dev = mate_device.MateDevice_(
            homey=make_homey(api=api, settings={SETTING_HOME_SEQ: "5"}),
            store=_MATE_STORE, capabilities=["onoff"], name="안방 매트")
        await dev.on_init()
        await stop(dev)
        dev._mat = object()                 # non-None, so only `_closing` can refuse
        dev._reports = 0

        dev._on_reported("MAT-1", {"status": {"power": 1}})

        assert dev._tasks == set()
        assert dev._reports == 0

    asyncio.run(scenario())


# Link events — the cloud says the appliance went away, minutes before the poll would
#
# Captured on hardware (2026-08-03) by unplugging and replugging the AirOne. The frames are
# `{"topic": "event/rc/v2/1901/68FE710F2790043B/disconnected", "payload": {}, ...}` on the
# device's own MQTT topic, and they carry no `reported` section, so every one of them used
# to be dropped. Measured value: the `/connected` frame landed at 11:40:45 and the poll that
# would have restored the tile was not due until ~11:45:09.


class _Msg:
    """A paho message as `_on_message` reads it: `.topic` and raw `.payload` bytes."""

    def __init__(self, topic, payload, retain=False):
        import json

        self.topic = topic
        self.payload = json.dumps(payload).encode()
        self.retain = retain


def test_link_event_frame_is_routed_and_the_unknown_topic_still_is_not():
    """Routing, and the frame that must keep falling through.

    A second topic was seen on the same account — `{home}/airone/connected/{physId}`, payload
    literally `{}` — which arrives on every subscribe and whose semantics are unknown. It has
    no envelope `topic`, so it is not a link event and belongs where it already was: the
    unmatched log, which is what made the real event topic findable in the first place."""

    async def scenario():
        events, lines = [], []
        m = _mqtt(asyncio.get_running_loop(),
                  on_event=lambda device_id, connected: events.append((device_id, connected)))
        m._log = lines.append

        m._on_message(None, None, _Msg(
            "361954/airone/68FE710F2790043B",
            {"topic": "event/rc/v2/1901/68FE710F2790043B/disconnected",
             "payload": {}, "serviceCode": 300}))
        await asyncio.sleep(0)
        assert events == [("68FE710F2790043B", False)]
        assert lines == []

        m._on_message(None, None, _Msg("361954/airone/connected/68FE710F2790043B", {}))
        await asyncio.sleep(0)
        assert events == [("68FE710F2790043B", False)]     # nothing new
        assert any("unmatched frame" in line for line in lines)

    asyncio.run(scenario())


def test_link_event_marks_unavailable_without_a_poll(make_homey):
    """The point of the whole change: the verdict lands on the tile when the event arrives,
    not up to a poll interval later. No REST call is involved in reaching it."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw(connected=True)])
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._mqtt = FakeMqtt(connected=True)

        dev._on_mqtt_event("RC-77", False)
        await until(lambda: dev.available is False, "the event alone to grey the tile out")

        assert dev._connected_registry is False
        assert dev.unavailable_reason == "기기가 오프라인입니다"
        assert api.calls.get("list_devices") is None      # no poll took part in this
        assert api.calls.get("airone_command") is None    # and an absent unit is not probed

    asyncio.run(scenario())


def test_link_event_connected_re_asks_for_state(make_homey):
    """The appliance has just come back and what it is doing is unknown — the same reason
    `_after_connect` re-requests state after a resubscribe. Nothing else can fill the tile:
    the reply is the only source of live state."""

    async def scenario():
        api = FakeApi()
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._mqtt = FakeMqtt(connected=True)
        dev._connected_registry = False
        await dev._update_availability()
        assert dev.available is False

        dev._on_mqtt_event("RC-77", True)
        await until(lambda: api.calls.get("airone_command", 0) >= 1,
                    "the event to re-ask the appliance for its state")

        assert dev._connected_registry is True
        assert dev.available is True
        assert api.calls.get("list_devices") is None

    asyncio.run(scenario())


def test_a_poll_corrects_a_wrong_link_event(make_homey):
    """REST stays the authority. The event is the fast signal and the poll is the correcting
    one, so a spurious or missed event costs one cycle at most — which is what allows the
    device to act on a single frame at all."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw(connected=True)])
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._mqtt = FakeMqtt(connected=True)

        dev._on_mqtt_event("RC-77", False)
        await until(lambda: dev.available is False, "the event to grey the tile out")

        await dev._poll_once()

        assert dev._connected_registry is True
        assert dev.available is True

    asyncio.run(scenario())


def test_a_failed_device_list_does_not_erase_a_link_event(make_homey):
    """A failed read is not a statement about the appliance. The per-cycle reset to None
    means "this list did not mention the device", so it has to happen only once a list has
    actually arrived — otherwise an unreachable server silently un-does the one signal that
    said something about the hardware."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw(connected=True)])
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._mqtt = FakeMqtt(connected=True)

        dev._on_mqtt_event("RC-77", False)
        await until(lambda: dev.available is False, "the event to grey the tile out")

        api.fail["list_devices"] = NavienNetworkError("no route to host")
        await dev._poll_once()

        assert dev._connected_registry is False
        assert dev.unavailable_reason == "기기가 오프라인입니다"

    asyncio.run(scenario())


def test_a_device_list_reading_that_predates_a_link_event_does_not_win(make_homey):
    """The reply describes the world from *before* the event that overtook it.

    Found in review. Guarding only the exception path left the stale-success path wide
    open: the poll awaits `list_devices`, a `/disconnected` lands while it is in flight,
    and then the reply — a snapshot the cloud took before the appliance dropped — resets
    the registry and writes `connected=1` back over it. The tile returns to available for
    a full cycle, which is exactly the wait acting on the event was meant to remove. The
    `/connected` mirror is the more visible one: a device we *know* is back gets greyed
    out again by a body that still said 0."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw(connected=True)])
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._mqtt = FakeMqtt(connected=True)

        # Fire the event from inside the in-flight window, which is what makes the reply
        # older than the verdict rather than newer.
        released = asyncio.Event()

        async def slow_list(_home_seq):
            dev._on_mqtt_event("RC-77", False)
            released.set()
            return [_airone_raw(connected=True)]

        api.list_devices = slow_list
        await dev._poll_once()
        assert released.is_set()

        assert dev._connected_registry is False
        await until(lambda: dev.available is False, "the event verdict to survive the poll")
        assert any("predates a link event" in line for line in dev.logs)

        # …and the *next* cycle, whose request went out after the event, corrects freely.
        del api.list_devices                       # back to the real coroutine
        api.devices = [_airone_raw(connected=True)]
        await dev._poll_once()
        assert dev._connected_registry is True

    asyncio.run(scenario())


def test_a_retained_link_event_is_ignored(make_homey):
    """A retained frame is the broker replaying the last publish, not news about now.

    Found in review, and it is the one genuinely new failure mode acting on these frames
    opens. `_reconnect_loop` resubscribes on every blip, so a retained `/disconnected` left
    on the topic by a power cut last week would grey out a working appliance after each
    hiccup. Before this feature such a frame was inert — one log line."""

    async def scenario():
        events, lines = [], []
        m = _mqtt(asyncio.get_running_loop(),
                  on_event=lambda device_id, connected: events.append((device_id, connected)))
        m._log = lines.append
        frame = {"topic": "event/rc/v2/1901/68FE710F2790043B/disconnected",
                 "payload": {}, "serviceCode": 300}

        m._on_message(None, None, _Msg(
            "361954/airone/68FE710F2790043B", frame, retain=True))
        await asyncio.sleep(0)
        assert events == []
        assert any("ignoring retained link event" in line for line in lines)

        # The same frame live is acted on — the retain bit is the only difference.
        m._on_message(None, None, _Msg("361954/airone/68FE710F2790043B", frame))
        await asyncio.sleep(0)
        assert events == [("68FE710F2790043B", False)]

    asyncio.run(scenario())


def test_a_superseded_connect_event_does_not_ask_for_state(make_homey):
    """`/connected` then `/disconnected` back to back: availability converges correctly
    because `_update_availability` reads live state, but the status probe used to read the
    captured argument and so POSTed a command to an appliance the same method had just
    declared offline. Unbounded, too — one POST per flap against an undocumented API."""

    async def scenario():
        api = FakeApi()
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._mqtt = FakeMqtt(connected=True)

        dev._on_mqtt_event("RC-77", True)
        dev._on_mqtt_event("RC-77", False)
        await until(lambda: dev.available is False, "the later verdict to win")

        assert dev._connected_registry is False
        assert api.calls.get("airone_command", 0) == 0

    asyncio.run(scenario())


def test_link_event_for_another_device_is_ignored(make_homey):
    """One MQTT client per device, but the subscription is a whole-home wildcard, so a
    sibling's event arrives here too. The physical id is cross-checked in the parser (topic
    against envelope) and again here against this device's own ids."""

    async def scenario():
        api = FakeApi()
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._mqtt = FakeMqtt(connected=True)

        dev._on_mqtt_event("SOMEONE-ELSE", False)
        await asyncio.sleep(0)

        assert dev._connected_registry is None
        assert dev.availability == []
        assert any("link event device_id='SOMEONE-ELSE' matched=False" in line
                   for line in dev.logs)

    asyncio.run(scenario())


# Instrumentation


def test_device_list_connected_is_logged_on_every_change(make_homey):
    """It used to be one-shot per boot, and that is why the 1 -> 0 transition during the
    hardware test left no trace: the flag had been spent on the first cycle's 1. A change is
    the only thing worth a line — an unchanged value 288 times a day is not."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw(connected=True)])
        dev = await _airone_at_rest(make_homey, api, _STATUS_CAPS)
        dev._mqtt = FakeMqtt(connected=True)

        await dev._poll_once()
        await dev._poll_once()                       # unchanged — no second line
        api.devices = [_airone_raw(connected=False)]
        await dev._poll_once()

        lines = [line for line in dev.logs if "device-list connected=" in line]
        assert len(lines) == 2
        assert "connected=True" in lines[0]
        assert "connected=False" in lines[1]

    asyncio.run(scenario())


def test_airone_logs_air_sensor_values_once_per_poll(make_homey):
    """Deliberate instrumentation for the open AirMonitor question (see
    `airmonitor/device.py`): changed values only, so a live feed is one short line and a
    frozen one reads as "unchanged" repeating."""

    async def scenario():
        api = FakeApi(devices=[_airone_raw()],
                      sensors=[{"airs": [{"type": "pm2Dot5", "value": 12, "level": 1}]}])
        dev = await _airone_at_rest(make_homey, api, ["onoff"])

        await dev._poll_once()
        await dev._poll_once()

        lines = [line for line in dev.logs if line.startswith("navien: air-sensor")]
        assert lines == ["navien: air-sensor pm25=12", "navien: air-sensor unchanged"]

    asyncio.run(scenario())


def test_airmonitor_logs_air_sensor_values_once_per_poll(make_homey):
    """The same line off the same endpoint, which is what makes the two comparable — and
    comparing them is how the "does the monitor report through its parent?" question gets
    settled."""

    async def scenario():
        api = FakeApi(sensors=[{"zoneId": "2",
                                "airs": [{"type": "pm2Dot5", "value": 12, "level": 1}]}])
        dev = airmonitor_device.AirMonitorDevice_(
            homey=make_homey(api=api), store=_MONITOR_STORE,
            capabilities=["measure_pm25"], name="거실 에어모니터")
        await dev.on_init()
        await stop(dev)                      # park the loop; drive the call directly
        dev._api = api

        await dev._poll_once()
        await dev._poll_once()

        lines = [line for line in dev.logs if line.startswith("navien: air-sensor")]
        assert lines == ["navien: air-sensor pm25=12", "navien: air-sensor unchanged"]

    asyncio.run(scenario())


def test_the_airone_hands_the_event_callback_to_its_client(make_homey, monkeypatch):
    """The parser and the handler are each covered above, and neither is reachable if the
    client is never given the callback — an omission there would leave every other test in
    this section green while no event ever arrived."""
    monkeypatch.setattr(NavienMqtt, "connect_blocking", lambda self: None)
    monkeypatch.setattr(NavienMqtt, "close", lambda self: None)

    async def scenario():
        api = FakeApi(aws=AwsCredentials("key", "secret", "token"))
        dev = await _airone_at_rest(make_homey, api, ["onoff"])

        await dev._start_mqtt()

        assert dev._mqtt._on_event == dev._on_mqtt_event
        await dev._teardown()

    asyncio.run(scenario())
