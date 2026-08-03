r"""REST client for the Navien Smart cloud.

Ported from navien_smart_ha's `api.py`. Two-step login (form login → secured
sign-in) yields an access token, the two distinct sequence numbers, and the
temporary AWS IoT credentials the MQTT layer needs. Device control is REST:
the app posts a command and the server relays it to the appliance and pushes the
resulting state back over MQTT.

The stdlib (`urllib`) is used rather than aiohttp so the only runtime dependency
stays `paho-mqtt`. Blocking calls are pushed to a thread via `run_in_executor`.

WHY the odd bits:
  * Two sequence numbers. The form login's `userSeq` is the *account* seq (used as
    `accountSeq` in the sign-in body); the sign-in's `userInfo.userSeq` is the
    *user* seq (used in REST query strings and the MQTT clientId). They differ.
  * One session per account. Opening the phone app invalidates ours, so any request
    can come back 404/407; `_authed` transparently re-logs-in once and retries.
  * AWS creds refresh only via a fresh secured-sign-in — /auth/token/refresh returns
    an access token but no AWS credentials.
  * The `topic` strings in a control body must be sent with their slashes escaped as
    `\/` in the raw JSON, or the server drops the command.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar

from navien_lib.const import (
    AIRONE_TOPIC_FMT,
    API_URL,
    AWS_CREDS_FRESH_S,
    CODE_NOT_AUTHORIZED,
    CODE_SUCCESS,
    CODE_TOKEN_EXPIRED,
    LOGIN_URL,
    USER_AGENT,
)
from navien_lib.navien import tls

# How long an air-sensor reading is reused, so an AirOne and its AirMonitor (which poll
# the same endpoint each cycle) don't each hit the server. Well under the poll interval.
AIR_SENSOR_CACHE_S = 60


class NavienAuthError(Exception):
    """Login failed for a reason retrying will not fix (bad password, etc.)."""


class NavienApiError(Exception):
    """A request failed after the one permitted re-login retry.

    `code` is the code the server put in its response envelope (or the raw HTTP status
    when the body carried none), so callers can tell a verdict the server will repeat —
    a 400 on a malformed or unsupported command — from one that a retry can change.
    It stays None whenever no server verdict exists, which is every NavienNetworkError:
    a caller testing `code == CODE_BAD_REQUEST` therefore cannot mistake an unreachable
    server for a permanent rejection, and network failures keep being retried.
    """

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


LOGGED_OUT = "로그아웃되었습니다. 앱 설정에서 나비엔 계정으로 다시 로그인하세요."


class NavienNetworkError(NavienApiError):
    """The request never reached the server (DNS, TCP, TLS, timeout).

    Subclassing NavienApiError rather than NavienAuthError is deliberate: every existing
    `except NavienApiError` keeps catching it, and the settings page routes it to
    "연결에 실패했습니다" (api.py:141-146) instead of telling the user their password is
    wrong. Unlike NavienAuthError, retrying this one can fix it.
    """


@dataclass
class AwsCredentials:
    access_key_id: str
    secret_key: str
    session_token: str

    @classmethod
    def from_auth_info(cls, auth: dict) -> AwsCredentials | None:
        if not isinstance(auth, dict):
            return None
        key = auth.get("accessKeyId")
        secret = auth.get("secretKey") or auth.get("secretAccessKey")
        token = auth.get("sessionToken")
        if not (key and secret and token):
            return None
        return cls(key, secret, token)


@dataclass
class NavienApi:
    username: str
    password: str
    log: object = print

    access_token: str = ""
    account_seq: str = ""            # form login userSeq → sign-in accountSeq
    user_seq: str = ""               # sign-in userInfo.userSeq → REST query / clientId
    homes: list = field(default_factory=list)
    aws: AwsCredentials | None = None
    # Set by NavienApp.logout() on the *existing* object, because that object is the only
    # seam the app has on a running device: `homey` exposes no device registry (there is no
    # get_devices/get_driver anywhere in the surface this app can reach), and every device
    # caches the session it got from `_acquire_api` and never re-fetches it. Flipping this
    # is therefore what actually stops a logged-out account's traffic.
    disabled: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Monotonic login counter + when it last advanced, for `login_if_stale`. Two devices
    # that both see a 404 in the same instant would otherwise each re-login, and the second
    # login invalidates the first one's session — the very failure the retry exists to fix.
    _auth_gen: int = 0
    _auth_at: float = 0.0
    _air_cache: dict = field(default_factory=dict)   # device_seq -> (fetched_at, sensorList)
    _opener: object = None
    # Test seam: replaces the socket, not the error handling. When set it is called as
    # transport(method, url, headers, body, allow_redirects) -> (status, text) from
    # inside `_http`'s try, so a transport raising URLError still exercises the real
    # NavienNetworkError conversion. Every construction site is keyword-only (app.py:45,
    # api.py:65, :140, compat.py:73-77, :93-94), so appending a field is safe.
    transport: object = None

    def __post_init__(self) -> None:
        # The opener used to be built lazily inside `_http`, which runs in an executor
        # thread — two requests starting together could each build one and the cookie jar
        # carrying the login session would silently split in two. Build it once, here.
        # Every caller passes allow_redirects=True, so one opener covers them all.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()),
            # An HTTPSHandler bound to certifi's CA bundle — the runtime has no system CA
            # store, so the default handler would fail every request with
            # CERTIFICATE_VERIFY_FAILED.
            urllib.request.HTTPSHandler(context=tls.ssl_context()),
        )

    # --- public API --------------------------------------------------------

    @property
    def auth_gen(self) -> int:
        """The current login generation, to be captured *before* a request and handed back
        to `login_if_stale`. Public so the device layer can dedup without touching a
        private field."""
        return self._auth_gen

    def _refuse_if_disabled(self) -> None:
        if self.disabled:
            raise NavienApiError(LOGGED_OUT)

    async def login(self) -> None:
        """Run the full two-step login, populating tokens/seqs/AWS creds/homes."""
        self._refuse_if_disabled()
        async with self._lock:
            await self._login_locked()

    async def login_if_stale(self, gen: int) -> None:
        """Log in unless someone else already did it, recently, on our behalf.

        `gen` is the generation the caller saw before it made its request. Under the lock,
        a generation that has since advanced means another caller has already replaced the
        session we were about to replace, so logging in again would only invalidate theirs.

        The freshness window is not optional. `_login_locked` re-mints `self.aws` every
        time and `build_signed_ws_path` (mqtt.py) sets no `X-Amz-Expires`, so skipping on
        generation alone would let a reconnect presign from arbitrarily old temporary
        credentials. Beyond AWS_CREDS_FRESH_S an advanced generation is re-minted anyway:
        the dedup only has to cover callers that raced, and those are seconds apart.
        """
        self._refuse_if_disabled()
        async with self._lock:
            if self._auth_gen > gen and (time.monotonic() - self._auth_at) < AWS_CREDS_FRESH_S:
                return
            await self._login_locked()

    async def _login_locked(self) -> None:
        token_msg = await self._run(self._form_login)
        self.access_token = token_msg["accessToken"]
        login_id = token_msg["loginId"]
        # Keep userSeq's original type (an int) for the sign-in body — stringifying it
        # makes secured-sign-in return 500.
        account_seq = token_msg["userSeq"]
        self.account_seq = str(account_seq)

        data = await self._run(self._secured_sign_in, login_id, account_seq)
        self.homes = data.get("home") or data.get("homes") or []
        self.user_seq = str((data.get("userInfo") or {}).get("userSeq") or self.account_seq)
        self.aws = AwsCredentials.from_auth_info(data.get("authInfo") or {})
        # Bumped only once both legs succeeded, so a failed login never makes a waiting
        # `login_if_stale` believe fresh credentials exist.
        self._auth_gen += 1
        self._auth_at = time.monotonic()
        self.log(f"navien: logged in (user_seq={self.user_seq}, homes={len(self.homes)})")

    def home_seqs(self) -> list:
        """`(home_seq, label)` for each home on the account."""
        out = []
        for home in self.homes:
            seq = home.get("homeSeq") or home.get("seq")
            if seq is None:
                continue
            label = home.get("homeName") or home.get("name") or str(seq)
            out.append((int(seq), label))
        return out

    async def list_devices(self, home_seq: int) -> list:
        """Raw device dicts for a home, straight from `GET /devices`."""
        data = await self._authed(
            "GET", f"/devices?{self._q(home_seq)}"
        )
        return (data or {}).get("devices") or []

    async def airone_command(
        self, *, device_seq, home_seq, model_code, physical_device_id,
        client_id, command, desired=None,
    ) -> dict:
        """POST an AirOne command; the resulting state arrives later over MQTT."""
        topic = AIRONE_TOPIC_FMT.format(
            model_code=model_code, physical_device_id=physical_device_id, command=command
        )
        response_topic = f"{topic}/res"
        payload = {
            "clientId": client_id,                     # so the server routes the reply back
            "sessionId": str(int(time.time() * 1000)),
            "requestTopic": topic,
            "responseTopic": response_topic,
        }
        if desired is not None:
            payload["state"] = {"desired": desired}
        body = {"serviceCode": 300, "payload": payload}
        raw = self._escape_topics(json.dumps(body, ensure_ascii=False),
                                  response_topic, topic)
        return await self._authed(
            "POST", f"/devices/{device_seq}/control?{self._q(home_seq)}", raw_body=raw
        )

    async def mate_control(self, *, device_seq, home_seq, device_id, model_code,
                           service_code, desired) -> dict:
        r"""POST a sleep-mat command, relayed to the device's AWS shadow.

        `event.modelCode` (int) is attached to every command, and the shadow topic's
        slashes must go out as `\/` in the raw JSON — built via a sentinel so only the
        topic value is escaped, exactly as the app does.
        """
        topic = f"$aws/things/{device_id}/shadow/name/status/update"
        body = {
            "serviceCode": service_code,
            "topic": "\x00TOPIC\x00",
            "payload": {"state": {"desired": {
                "event": {"modelCode": int(model_code)},
                **desired,
            }}},
        }
        raw = json.dumps(body, ensure_ascii=False).replace(
            '"\\u0000TOPIC\\u0000"', json.dumps(topic).replace("/", "\\/"))
        return await self._authed(
            "POST", f"/devices/{device_seq}/control?{self._q(home_seq)}", raw_body=raw
        )

    async def air_sensor(self, device_seq, home_seq) -> list:
        """Air-quality readings. These come from REST only — MQTT carries the sensor
        kinds but not their values.

        Cached briefly: an AirOne and its AirMonitor both poll the *same* endpoint each
        cycle, so without this the account makes two identical requests. The TTL is well
        under the poll interval, so each cycle still gets fresh data.
        """
        key = str(device_seq)
        hit = self._air_cache.get(key)
        if hit is not None and (time.time() - hit[0]) < AIR_SENSOR_CACHE_S:
            return hit[1]
        data = await self._authed(
            "GET", f"/devices/{device_seq}/air-sensor?{self._q(home_seq)}"
        )
        sensor_list = (data or {}).get("sensorList") or []
        self._air_cache[key] = (time.time(), sensor_list)
        return sensor_list

    # --- request plumbing --------------------------------------------------

    def _q(self, home_seq: int) -> str:
        return urllib.parse.urlencode({"homeSeq": home_seq, "userSeq": self.user_seq})

    @staticmethod
    def _escape_topics(raw: str, response_topic: str, topic: str) -> str:
        # The server expects `\/` in the topic strings. Replace the longer
        # (responseTopic) first so the shorter (topic) doesn't corrupt its `/res` tail.
        for t in (response_topic, topic):
            raw = raw.replace(t, t.replace("/", "\\/"))
        return raw

    async def _authed(self, method, path, raw_body=None) -> dict:
        """Authenticated API call with two independent retry budgets.

        A network failure and an invalidated session are different conditions, and the
        old single `for attempt in (1, 2)` counter could not express both at once: a
        blip consumed the only attempt, so the 404 that followed had no re-login left.
        Two counters keep the budget hard-capped at 3 HTTP requests and 1 re-login.
        """
        # Checked before anything else so a logged-out account makes zero HTTP traffic:
        # devices keep their reference to this object forever, so this is the only place
        # that can stop them (see `disabled`).
        self._refuse_if_disabled()
        if not self.access_token:
            await self.login_if_stale(self._auth_gen)
        net_retries, relogins = 1, 1
        while True:
            # Captured before the request, not after it: the point of the generation is to
            # tell "nobody has logged in since I decided I needed one" from "somebody
            # already has", and only a pre-request snapshot can say that.
            gen = self._auth_gen
            try:
                status, text = await self._run(
                    self._http, method, API_URL + path, self._auth_headers(), raw_body, True
                )
            except NavienNetworkError:
                if net_retries <= 0:
                    raise
                net_retries -= 1
                await asyncio.sleep(1)
                continue
            envelope = self._parse_envelope(text)
            code = envelope.get("code", status)
            if code == CODE_SUCCESS:
                return envelope.get("data") or {}
            if code in (CODE_NOT_AUTHORIZED, CODE_TOKEN_EXPIRED) and relogins > 0:
                relogins -= 1
                self.log(f"navien: {code} on {path}; re-logging in")
                await self.login_if_stale(gen)
                continue
            # The code travels with the error, not just inside its message string: the
            # control path has to decide whether re-sending can help, and parsing that
            # back out of a formatted message would be the kind of guess this raise exists
            # to replace.
            raise NavienApiError(
                f"{method} {path} -> code {code}: {envelope.get('message')}", code
            )

    def _auth_headers(self) -> dict:
        return {
            "Authorization": self.access_token,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _parse_envelope(text: str) -> dict:
        try:
            data = json.loads(text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    # --- login steps (blocking, run in executor) ---------------------------

    def _form_login(self) -> dict:
        """Step 1: form login. Returns the token message parsed from the HTML."""
        body = urllib.parse.urlencode(
            {"username": self.username, "password": self.password}
        ).encode()
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": LOGIN_URL,
            "Referer": f"{LOGIN_URL}/member/login",
        }
        _status, html = self._http("POST", f"{LOGIN_URL}/member/login", headers, body, True)
        # An empty body is a truncated connection, not a rejected account. Without this
        # it falls through to the NavienAuthError below, which is documented as
        # "retrying will not fix" and is surfaced to the user as a credentials problem.
        if not html.strip():
            raise NavienNetworkError("빈 로그인 응답")
        if 'id="loginFailPopup"' in html:
            raise NavienAuthError("아이디 또는 비밀번호가 올바르지 않습니다.")
        if "passwordChg" in html:
            raise NavienAuthError("비밀번호 변경이 필요합니다. 앱에서 변경 후 다시 시도하세요.")
        message = self._extract_message_json(html)
        if not message or "accessToken" not in message:
            raise NavienAuthError("로그인 응답을 해석하지 못했습니다.")
        return message

    @staticmethod
    def _extract_message_json(html: str) -> dict | None:
        """Parse the `var message = {...}` token blob from the login HTML.

        Scans the line from its first `{` to its last `}` (not a non-greedy regex) so
        nested objects in the blob don't truncate it.
        """
        for line in html.splitlines():
            if "var message" not in line:
                continue
            start = line.find("{")
            end = line.rfind("}")
            if start == -1 or end <= start:
                continue
            try:
                return json.loads(line[start:end + 1])
            except Exception:
                continue
        return None

    def _secured_sign_in(self, login_id: str, account_seq) -> dict:
        headers = {
            "Authorization": self.access_token,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps({"userId": login_id, "accountSeq": account_seq}).encode()
        _status, text = self._http(
            "POST", f"{API_URL}/users/secured-sign-in", headers, body, True
        )
        # The silent half. `_parse_envelope("")` is `{}`, and `{}.get("code", CODE_SUCCESS)`
        # equals CODE_SUCCESS, so an empty body used to sail through the check below:
        # `_login_locked` then set homes=[] and aws=None and logged "navien: logged in",
        # after which `_start_mqtt` bailed on the missing credentials and realtime push
        # was off app-wide behind a success message.
        if not text.strip():
            raise NavienNetworkError("빈 secured-sign-in 응답")
        envelope = self._parse_envelope(text)
        if envelope.get("code", CODE_SUCCESS) != CODE_SUCCESS:
            raise NavienAuthError(f"secured-sign-in 실패: {envelope.get('message')}")
        return envelope.get("data") or {}

    # --- raw HTTP ----------------------------------------------------------

    def _http(self, method, url, headers, body, allow_redirects) -> tuple:
        """One blocking HTTP request. Returns (status, response_text).

        A shared cookie jar keeps the login session; 4xx/5xx bodies are read too,
        because the API returns its status inside a 200-shaped envelope and the login
        endpoint answers with HTML either way. A request that never reached the server
        raises NavienNetworkError instead, so callers can tell "the server said no" from
        "we could not ask" — the two need opposite handling.
        """
        if isinstance(body, str):
            # Control payloads (the \/-escaped raw bodies) arrive as str; urllib needs bytes.
            body = body.encode("utf-8")
        self.log(f"navien http: {method} {url}")
        try:
            if self.transport is not None:
                return self.transport(method, url, headers, body, allow_redirects)
            req = urllib.request.Request(url, data=body, method=method)
            for key, value in headers.items():
                req.add_header(key, value)
            with self._opener.open(req, timeout=15) as resp:
                self.log(f"navien http: {method} {url} -> {resp.status}")
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # This clause is load-bearing where it is: HTTPError subclasses URLError, so
            # moving it below the next one would reclassify every 4xx/5xx as a network
            # failure and lose the envelope inside the 4xx body.
            self.log(f"navien http: {method} {url} -> HTTPError {exc.code}")
            return exc.code, exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            self.log(f"navien http: {method} {url} -> network error {exc}")
            raise NavienNetworkError(str(exc)) from exc

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)
