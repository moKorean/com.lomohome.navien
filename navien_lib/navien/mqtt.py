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

    AirOne messages are `{"reported": {...}}` (no shadow `state` wrapper). At least one
    of the known sections must be present, so an empty/ack frame doesn't get HA ahead
    of the device.
    """
    reported = (payload or {}).get("reported")
    if not isinstance(reported, dict):
        return None
    if not any(k in reported for k in ("roomController", "odu", "airMonitor", "idu")):
        return None
    device_id = (reported.get("roomController") or {}).get("deviceId") or topic.rsplit("/", 1)[-1]
    return device_id, reported


class NavienMqtt:
    """Subscribe-only AWS IoT client that pushes AirOne reports to a callback.

    `creds_provider()` returns the current AwsCredentials (re-fetched on reconnect,
    because the presigned path expires). `on_reported(device_id, reported)` is invoked
    on the asyncio loop.
    """

    def __init__(self, *, loop, user_seq, home_seq, creds_provider, on_reported,
                 prefixes=("airone",), parser=extract_airone_reported, log=print):
        self._loop = loop
        self._user_seq = user_seq
        self._home_seq = home_seq
        self._creds_provider = creds_provider
        self._on_reported = on_reported
        self._prefixes = tuple(prefixes)
        self._parser = parser
        self._log = log
        self._client = None
        self._client_id = ""
        self._connected = False

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def connected(self) -> bool:
        return self._connected

    def connect_blocking(self) -> None:
        """Build a client and connect. Runs in an executor thread (blocking)."""
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
        self._client = client
        client.connect(IOT_ENDPOINT, IOT_PORT, keepalive=60)
        client.loop_start()

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    # --- paho callbacks (paho thread) --------------------------------------

    def _topics(self) -> list:
        # `#` because replies arrive one level deeper than the prefix.
        return [f"{self._home_seq}/{prefix}/#" for prefix in self._prefixes]

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None):
        if getattr(reason_code, "is_failure", False):
            self._log(f"navien mqtt: connect failed ({reason_code})")
            return
        for topic in self._topics():
            client.subscribe(topic, qos=0)
        self._connected = True
        self._log(f"navien mqtt: connected, subscribed {self._topics()}")

    def _on_disconnect(self, _client, _userdata, *args):
        self._connected = False
        self._log("navien mqtt: disconnected")

    def _on_message(self, _client, _userdata, message):
        import json

        try:
            payload = json.loads(message.payload.decode("utf-8", "replace"))
        except Exception:
            return
        parsed = self._parser(message.topic, payload)
        if parsed is None:
            return
        device_id, reported = parsed
        # Hop off the paho thread before touching Homey/asyncio state.
        self._loop.call_soon_threadsafe(self._on_reported, device_id, reported)
