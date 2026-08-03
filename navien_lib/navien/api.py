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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar

from navien_lib.const import (
    AIRONE_TOPIC_FMT,
    API_URL,
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
    """A request failed after the one permitted re-login retry."""


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
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _air_cache: dict = field(default_factory=dict)   # device_seq -> (fetched_at, sensorList)

    # --- public API --------------------------------------------------------

    async def login(self) -> None:
        """Run the full two-step login, populating tokens/seqs/AWS creds/homes."""
        async with self._lock:
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
        """Authenticated API call with one transparent re-login on 404/407."""
        if not self.access_token:
            await self.login()
        for attempt in (1, 2):
            status, text = await self._run(
                self._http, method, API_URL + path, self._auth_headers(), raw_body, True
            )
            envelope = self._parse_envelope(text)
            code = envelope.get("code", status)
            if code == CODE_SUCCESS:
                return envelope.get("data") or {}
            if code in (CODE_NOT_AUTHORIZED, CODE_TOKEN_EXPIRED) and attempt == 1:
                self.log(f"navien: {code} on {path}; re-logging in")
                await self.login()
                continue
            raise NavienApiError(f"{method} {path} -> code {code}: {envelope.get('message')}")
        raise NavienApiError(f"{method} {path} failed after re-login")

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
        envelope = self._parse_envelope(text)
        if envelope.get("code", CODE_SUCCESS) != CODE_SUCCESS:
            raise NavienAuthError(f"secured-sign-in 실패: {envelope.get('message')}")
        return envelope.get("data") or {}

    # --- raw HTTP ----------------------------------------------------------

    def _http(self, method, url, headers, body, allow_redirects) -> tuple:
        """One blocking HTTP request. Returns (status, response_text).

        A shared cookie jar keeps the login session; 4xx/5xx bodies are read too,
        because the API returns its status inside a 200-shaped envelope and the login
        endpoint answers with HTML either way.
        """
        if not hasattr(self, "_opener"):
            # An HTTPSHandler bound to certifi's CA bundle — the runtime has no system
            # CA store, so the default handler would fail every request with
            # CERTIFICATE_VERIFY_FAILED.
            handlers = [
                urllib.request.HTTPCookieProcessor(CookieJar()),
                urllib.request.HTTPSHandler(context=tls.ssl_context()),
            ]
            if not allow_redirects:
                handlers.append(_NoRedirect())
            self._opener = urllib.request.build_opener(*handlers)
        if isinstance(body, str):
            # Control payloads (the \/-escaped raw bodies) arrive as str; urllib needs bytes.
            body = body.encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method)
        for key, value in headers.items():
            req.add_header(key, value)
        self.log(f"navien http: {method} {url}")
        try:
            with self._opener.open(req, timeout=15) as resp:
                self.log(f"navien http: {method} {url} -> {resp.status}")
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            self.log(f"navien http: {method} {url} -> HTTPError {exc.code}")
            return exc.code, exc.read().decode("utf-8", "replace")

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
