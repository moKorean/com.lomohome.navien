"""Realtime state push over AWS IoT MQTT-over-WebSocket.

Ported from navien_smart_ha's `mqtt.py`. The AirOne pushes its state to an AWS IoT
topic; this subscribes to it. It never publishes — control goes through REST
(`api.py`) and the resulting state comes back here.

Connecting needs a SigV4-presigned WebSocket path built from the temporary AWS
credentials that the REST login handed back. paho runs its network loop on its own
thread, so incoming messages are marshalled onto the asyncio loop with
`call_soon_threadsafe` before the state callback runs.

WHY the endpoint constant and not the device's: the broker is Navien's own
`nskr-iot...` domain; the `endpoint` in a device's registry is where the *appliance*
connects and using it gives an SNI mismatch. See docs/PORTING.md.
"""

import hashlib
import hmac
import time
import urllib.parse
import uuid

from navien_lib.const import AWS_REGION, AWS_SERVICE, IOT_ENDPOINT, IOT_PORT
from navien_lib.navien import tls


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("AWS4" + secret).encode(), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def build_signed_ws_path(creds, host: str = IOT_ENDPOINT,
                         region: str = AWS_REGION, service: str = AWS_SERVICE) -> str:
    """A SigV4-presigned `/mqtt?...` path for the AWS IoT WebSocket handshake."""
    algorithm = "AWS4-HMAC-SHA256"
    amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    datestamp = amz_date[:8]
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    canonical_uri = "/mqtt"

    # Query params, sorted by name, WITHOUT the signature or token yet.
    query = "&".join([
        "X-Amz-Algorithm=" + algorithm,
        "X-Amz-Credential=" + urllib.parse.quote(f"{creds.access_key_id}/{scope}", safe=""),
        "X-Amz-Date=" + amz_date,
        "X-Amz-SignedHeaders=host",
    ])
    canonical_headers = f"host:{host}\n"
    empty_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = "\n".join(
        ["GET", canonical_uri, query, canonical_headers, "host", empty_hash]
    )
    string_to_sign = "\n".join(
        [algorithm, amz_date, scope,
         hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    signature = hmac.new(
        _signing_key(creds.secret_key, datestamp, region, service),
        string_to_sign.encode(), hashlib.sha256,
    ).hexdigest()

    # The signature and the security token are appended AFTER signing.
    query += "&X-Amz-Signature=" + signature
    query += "&X-Amz-Security-Token=" + urllib.parse.quote(creds.session_token, safe="")
    return f"{canonical_uri}?{query}"


def extract_airone_reported(topic: str, payload: dict):
    """`(device_id, reported)` from an AirOne message, or None if it isn't one.

    The envelope is `{"topic": ..., "payload": {"reported": {...}}, "serviceCode": ...}`,
    so the reported state is nested one level inside `payload`. At least one known section
    must be present, so an empty/ack frame doesn't get us ahead of the device. The MQTT
    message topic's last segment is the device id (used when roomController omits it).
    """
    inner = (payload or {}).get("payload")
    if not isinstance(inner, dict):
        inner = payload or {}
    reported = inner.get("reported")
    if not isinstance(reported, dict):
        return None
    if not any(k in reported for k in ("roomController", "odu", "airMonitor", "idu")):
        return None
    device_id = (reported.get("roomController") or {}).get("deviceId") or topic.rsplit("/", 1)[-1]
    return device_id, reported


# The two envelope `topic` suffixes that state a link verdict, and what each one means.
_EVENT_STATES = {"connected": True, "disconnected": False}


def extract_connection_event(topic: str, payload: dict):
    """`(physical_device_id, connected)` from a link event, or None if it isn't one.

    Captured on real hardware (2026-08-03) by unplugging and replugging the AirOne. The
    MQTT topic is `{home_seq}/airone/{physical_device_id}` and the payload is::

        {"topic": "event/rc/v2/1901/68FE710F2790043B/disconnected",
         "payload": {}, "serviceCode": 300}

    with the same envelope and a `/connected` suffix when power came back. There is no
    `reported` section, so `extract_airone_reported` drops these frames and every one of
    them used to land in the unmatched-frame log.

    WHY this parses the envelope only, and nothing about what the device *is*: the verdict
    it carries is about the link, so the device layer can act on it without this function
    knowing anything about AirOne state. That is also why it lives next to the SigV4 helper
    rather than in `airone.py`.

    The physical id is in two places — the MQTT topic's last segment and the second-to-last
    segment of the envelope's own `topic` — and they are cross-checked rather than one being
    trusted. A disagreement means this is some other frame whose shape happens to end in the
    same word, and the caller ignores it exactly as it ignores anything else it cannot read.

    NOT to be confused with `{home_seq}/airone/connected/{physical_device_id}`, a different
    topic seen on the same account whose payload is literally `{}`. It arrives on every
    subscribe (it looks retained), carries no data at all, and its semantics are unknown —
    it has no envelope `topic` key, so it returns None here and keeps falling through to the
    unmatched log, which is where an unexplained frame belongs.
    """
    inner_topic = (payload or {}).get("topic")
    if not isinstance(inner_topic, str):
        return None
    parts = inner_topic.strip("/").split("/")
    if len(parts) < 2:
        return None
    connected = _EVENT_STATES.get(parts[-1])
    if connected is None:
        return None
    device_id = parts[-2]
    if not device_id or device_id != topic.rsplit("/", 1)[-1]:
        return None
    return device_id, connected


class NavienMqtt:
    """Subscribe-only AWS IoT client that pushes AirOne reports to a callback.

    `creds_provider()` returns the current AwsCredentials (re-fetched on reconnect,
    because the presigned path expires). `home_seq_provider()` is the same shape and exists
    for the same reason: the home a device belongs to is read from settings and the settings
    page rewrites it, so a value captured at construction would keep this client subscribed
    to a tree the account no longer uses. `on_reported(device_id, reported)` is invoked on
    the asyncio loop, and so are the optional `on_connected()` / `on_disconnected()`, which
    exist so the device layer can rebuild the link on the event instead of on the next poll
    tick — paho's own reconnect retries the one presigned path it was given, and that path
    carries an STS token nothing can refresh from inside paho.

    `on_event(device_id, connected)` reports the *appliance's* link rather than ours, and it
    is optional in both halves for the same reason `parser` is injectable: a client handed
    no callback runs no event parsing and behaves exactly as it did before. That is
    deliberate — the frames were only ever observed on this account's AirOne topic, so the
    mat's client, which is not paired and cannot be tested against, keeps dropping anything
    it does not recognise rather than acting on a shape nobody has seen it send.
    """

    def __init__(self, *, loop, user_seq, home_seq_provider, creds_provider, on_reported,
                 prefixes=("airone",), parser=extract_airone_reported, log=print,
                 on_connected=None, on_disconnected=None, on_event=None,
                 event_parser=extract_connection_event):
        self._loop = loop
        self._user_seq = user_seq
        self._home_seq_provider = home_seq_provider
        self._creds_provider = creds_provider
        self._on_reported = on_reported
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_event = on_event
        self._prefixes = tuple(prefixes)
        self._parser = parser
        self._event_parser = event_parser
        self._log = log
        self._client = None
        self._client_id = ""
        self._connected = False
        # Set at the top of `close()` and cleared at the top of `connect_blocking()`.
        # It exists because `close()` calls `loop_stop()` before `disconnect()`: once
        # `loop_stop()` has joined paho's network thread, `_packet_queue` writes on the
        # *calling* thread, so `on_disconnect` fires synchronously inside `close()`.
        # Without this flag every close would look like a drop and re-arm the very
        # reconnect path that is dismantling the client.
        self._closing = False

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def connected(self) -> bool:
        return self._connected

    def connect_blocking(self) -> None:
        """Build a client and connect. Runs in an executor thread (blocking)."""
        # F13: `close()` leaves `_client` None but used to leave `_connected` True, and a
        # stale True with no client makes the device layer's `_ensure_mqtt` return early
        # for the rest of the app's life. Reset here as well as in `close()`, so a failed
        # attempt cannot leave the previous connection's verdict standing.
        self._connected = False
        # Cleared here rather than in a trailing `finally`, and the position is the fix.
        # The `finally` covered the raising path — the outage this reconnect exists for —
        # but it only ran *after* `loop_start()`, so a CONNACK that landed in between was
        # dispatched into a still-suppressing `_dispatch` and the `on_connected` callback
        # was swallowed: a connection that came up with nothing re-requesting state. Doing
        # it at the top covers the raising path just as well (nothing below re-sets it on
        # its own) and leaves the whole body running with the flag honest. It also gives
        # the two guards below a meaning they could not otherwise have: from here on,
        # `_closing` being True can only mean a `close()` landed *during* this build.
        self._closing = False

        import paho.mqtt.client as mqtt

        creds = self._creds_provider()
        self._client_id = f"{uuid.uuid4()}-U{self._user_seq}"
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            transport="websockets",
        )
        client.tls_set_context(tls.ssl_context())
        client.ws_set_options(path=build_signed_ws_path(creds))
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        # paho's own reconnect handles transient drops; a fresh presigned path is
        # rebuilt by reconnecting through the device layer when creds expire.
        client.reconnect_delay_set(min_delay=5, max_delay=300)
        # B4. The device layer's asyncio lock serialises the *coroutines* that drive this
        # method, but `asyncio.gather` on a cancelled task returns while the executor
        # thread underneath `_to_thread(connect_blocking)` keeps running — so teardown can
        # queue `close()` on another pool worker mid-build. Installing the client anyway
        # would `loop_start()` a network thread that outlives the device and that nothing
        # holds a reference to. Refusing at the point of assignment is what makes that
        # unreachable; there is nothing to unwind here, because the client has not
        # connected yet.
        if self._closing:
            self._log("navien mqtt: close() landed mid-connect; client not installed")
            return
        self._client = client
        client.connect(IOT_ENDPOINT, IOT_PORT, keepalive=60)
        client.loop_start()
        # The other half of the same guard, and both are needed. A `close()` landing
        # between the check above and `loop_start()` finds `_client` already set, so it
        # calls `loop_stop()` on a loop that has not started yet and then this line starts
        # it — the orphan the check above exists to prevent, one statement later. Undoing
        # it here is the only way to catch that ordering, since a close() this late has
        # already done its own work.
        if self._closing:
            self._log("navien mqtt: close() landed mid-connect; unwinding the client")
            self._connected = False
            self._client = None
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

    def close(self) -> None:
        # Before loop_stop()/disconnect(), because `_on_disconnect` fires synchronously
        # inside this call once the network thread is gone (see __init__).
        self._closing = True
        self._connected = False
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    # --- paho callbacks (paho thread) --------------------------------------

    def _topics(self) -> list:
        # `#` because replies arrive one level deeper than the prefix. The home_seq is read
        # per call, so a client rebuilt after the setting changed subscribes to the new tree.
        return [f"{self._home_seq_provider()}/{prefix}/#" for prefix in self._prefixes]

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None):
        if getattr(reason_code, "is_failure", False):
            self._log(f"navien mqtt: connect failed ({reason_code})")
            return
        for topic in self._topics():
            client.subscribe(topic, qos=0)
        self._connected = True
        self._log(f"navien mqtt: connected, subscribed {self._topics()}")
        # Dispatched *after* subscribe() and after `_connected = True`, and that ordering
        # is load-bearing rather than tidy: the device layer's re-request of state guards
        # itself on `mqtt.connected`, so a callback dispatched any earlier would arrive on
        # the loop while `connected` was still False and cancel the very request it exists
        # to make. It also means the subscription is in place before the reply can arrive.
        self._dispatch(self._on_connected)

    def _on_disconnect(self, _client, _userdata, *args):
        self._connected = False
        self._log("navien mqtt: disconnected")
        self._dispatch(self._on_disconnected)

    def _dispatch(self, callback) -> None:
        """Hop a paho-thread event onto the asyncio loop — the same hop `_on_message`
        makes. RuntimeError is the loop being closed already, i.e. app teardown.

        `_closing` is checked here so it covers both events: a deliberate `close()` must
        neither re-arm the reconnect (the disconnect it causes is not a drop) nor announce
        a connection on a client that is being taken apart.
        """
        if callback is None or self._closing:
            return
        try:
            self._loop.call_soon_threadsafe(callback)
        except RuntimeError:
            pass

    def _on_message(self, _client, _userdata, message):
        import json

        try:
            payload = json.loads(message.payload.decode("utf-8", "replace"))
        except Exception:
            return
        parsed = self._parser(message.topic, payload)
        if parsed is None:
            # A link event carries no `reported` section by design, so it can only be
            # recognised after the state parser has passed on the frame. Only attempted
            # when someone asked for these events; without a callback this is the same
            # code path it always was.
            if self._on_event is not None:
                event = self._event_parser(message.topic, payload)
                if event is not None:
                    # A retained frame is the broker replaying the last thing published on
                    # this topic, not news about now — and `_reconnect_loop` resubscribes on
                    # every blip, so acting on one would re-apply a week-old `/disconnected`
                    # and grey out a working appliance after each hiccup. Dropping it costs
                    # nothing: the boot verdict already comes from REST `connected`, which is
                    # read on the first poll. Logged rather than silently skipped, because
                    # whether these frames are retained at all is not yet known — the sibling
                    # `.../connected/{id}` topic looks retained and this one may or may not be.
                    if getattr(message, "retain", False):
                        self._log(f"navien mqtt: ignoring retained link event {message.topic}")
                        return
                    device_id, connected = event
                    self._loop.call_soon_threadsafe(self._on_event, device_id, connected)
                    return
            # I4. A frame that carries no known reported section is dropped here in
            # silence today, which is indistinguishable from no frame arriving at all —
            # the failure mode an empty tile has to be traced through.
            # The payload goes in truncated. Knowing *that* a frame was dropped only
            # tells you something is missing; knowing its shape is what lets the next
            # topic be parsed instead of guessed at — the `connected` topic was found
            # exactly this way.
            body = json.dumps(payload, ensure_ascii=False)
            self._log(f"navien mqtt: unmatched frame {message.topic} device_id=None "
                      f"matched=False (no reported section, no link event) "
                      f"payload={body[:400]}")
            return
        device_id, reported = parsed
        # Hop off the paho thread before touching Homey/asyncio state.
        self._loop.call_soon_threadsafe(self._on_reported, device_id, reported)
