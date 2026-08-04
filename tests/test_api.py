"""REST-plumbing tests for `NavienApi`, driven through the `transport` seam.

The seam (navien/api.py:107) replaces the socket, not the error handling: it is called
from *inside* `_http`'s `try` (:377-378), so a transport that raises `URLError` still goes
through the real `except` chain and the real NavienNetworkError conversion. That is the
whole point — these tests assert on the classification, and a seam placed outside the try
could not check it.

No event-loop plugin is installed (pytest-asyncio is not a dev dependency and this suite
does not add one), so each test drives its own loop with `asyncio.run`.
"""

import asyncio
import json
import threading
import time
import urllib.error
import urllib.parse

import pytest

from navien_lib import pairing
from navien_lib.const import (
    AWS_CREDS_FRESH_S,
    CODE_BAD_REQUEST,
    CODE_NOT_AUTHORIZED,
    CODE_SUCCESS,
    SETTING_HOME_SEQ,
    SETTING_PASSWORD,
    SETTING_USERNAME,
    STORE_DEVICE_ID,
    STORE_DEVICE_SEQ,
    STORE_MODEL_CODE,
    STORE_PHYSICAL_ID,
)
from navien_lib.navien.api import (
    NavienApi,
    NavienApiError,
    NavienAuthError,
    NavienNetworkError,
)

# A form-login response carrying the token blob `_extract_message_json` looks for.
ACCOUNT_SEQ = 4242
_LOGIN_HTML = (
    "<html><body><script>\n"
    "  var message = " + json.dumps(
        {"accessToken": "TOKEN-1", "loginId": "me@example.com", "userSeq": ACCOUNT_SEQ}
    ) + ";\n"
    "</script></body></html>"
)


def _envelope(code=CODE_SUCCESS, data=None, message="ok") -> str:
    return json.dumps({"code": code, "message": message, "data": data or {}})


class Transport:
    """A scripted stand-in for the socket.

    Each entry is either an exception to raise or a `(status, text)` tuple; the last entry
    is reused once the script runs out, so a test only scripts what it is asserting on.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []          # (method, url, body)

    def __call__(self, method, url, headers, body, allow_redirects):
        self.calls.append((method, url, body))
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        response = self.responses[index]
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def count(self) -> int:
        return len(self.calls)

    def body_for(self, needle: str):
        for _method, url, body in self.calls:
            if needle in url:
                return body
        return None


def _api(transport, *, logged_in=False) -> NavienApi:
    lines = []
    api = NavienApi(username="user@example.com", password="pw",
                    log=lambda m: lines.append(str(m)), transport=transport)
    api.log_lines = lines
    if logged_in:
        # Skip the login leg so `_authed`'s own budget is what the test measures.
        api.access_token = "TOKEN-1"
        api.user_seq = "77"
    return api


def _count_logins(api) -> list:
    """Replace the two-step login with a counter, so "how many re-logins" is assertable.

    The seam sits at `_login_locked`, not at `login`/`login_if_stale`, so the lock and the
    generation bookkeeping those two are built on stay real — which is what the dedup tests
    below actually measure. The counter mirrors what a real login does to the generation.
    """
    calls = []

    async def counting_login():
        calls.append(True)
        api.access_token = f"TOKEN-{len(calls) + 1}"
        api._auth_gen += 1
        api._auth_at = time.monotonic()

    api._login_locked = counting_login
    return calls


@pytest.fixture
def no_sleep(monkeypatch):
    """Record the network-retry backoff instead of really waiting a second for it."""
    slept = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        slept.append(delay)
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


# --- login classification ---------------------------------------------------


def test_form_login_network_failure_is_not_auth_error():
    """A login that never reached the server must not be reported as a bad password.

    NavienAuthError is documented as "retrying will not fix" (api.py:52) and the settings
    page turns it into a credentials message, so misclassifying a blip here tells the user
    their password is wrong.
    """
    transport = Transport(urllib.error.URLError("dns failure"))
    api = _api(transport)
    with pytest.raises(NavienNetworkError) as caught:
        asyncio.run(api.login())
    assert not isinstance(caught.value, NavienAuthError)
    assert isinstance(caught.value, NavienApiError)   # every `except NavienApiError` keeps it


def test_form_login_empty_body_is_not_auth_error():
    """An empty 200 is a truncated connection, not a rejected account."""
    transport = Transport((200, ""))
    api = _api(transport)
    with pytest.raises(NavienNetworkError):
        asyncio.run(api.login())
    assert transport.count == 1                       # never got to secured-sign-in


def test_secured_sign_in_empty_body_raises_and_leaves_aws_untouched():
    """The silent half: an empty secured-sign-in body used to sail through.

    `_parse_envelope("")` is `{}` and `{}.get("code", CODE_SUCCESS)` equals CODE_SUCCESS,
    so `_login_locked` went on to set homes=[] / aws=None and log "navien: logged in" —
    after which MQTT bailed on the missing credentials and realtime push was off app-wide
    behind a success message.
    """
    transport = Transport((200, _LOGIN_HTML), (200, ""))
    api = _api(transport)
    api.homes = ["untouched"]

    with pytest.raises(NavienNetworkError):
        asyncio.run(api.login())

    assert api.aws is None
    assert api.homes == ["untouched"]                 # not replaced by an empty list
    assert not any("logged in" in line for line in api.log_lines)


def test_two_step_login_passes_account_seq_as_int():
    """`accountSeq` must go out with userSeq's original type — stringifying it makes the
    real server answer 500, which no offline test would otherwise catch."""
    transport = Transport(
        (200, _LOGIN_HTML),
        (200, _envelope(data={"userInfo": {"userSeq": 77}, "home": [{"homeSeq": 5}]})),
    )
    api = _api(transport)
    asyncio.run(api.login())

    body = json.loads(transport.body_for("/users/secured-sign-in").decode())
    assert body["accountSeq"] == ACCOUNT_SEQ
    assert isinstance(body["accountSeq"], int) and not isinstance(body["accountSeq"], bool)
    assert api.account_seq == str(ACCOUNT_SEQ)        # kept as a str on the object itself
    assert api.user_seq == "77"


# --- _authed retry budgets --------------------------------------------------


def test_authed_budget_network_then_success(no_sleep):
    transport = Transport(
        urllib.error.URLError("connection reset"),
        (200, _envelope(data={"devices": [{"deviceId": "A"}]})),
    )
    api = _api(transport, logged_in=True)
    logins = _count_logins(api)

    assert asyncio.run(api.list_devices(1)) == [{"deviceId": "A"}]
    assert transport.count == 2
    assert logins == []                               # a blip is not an auth problem
    assert no_sleep == [1]


def test_authed_budget_network_twice(no_sleep):
    transport = Transport(urllib.error.URLError("connection reset"))
    api = _api(transport, logged_in=True)
    logins = _count_logins(api)

    with pytest.raises(NavienNetworkError):
        asyncio.run(api.list_devices(1))
    assert transport.count == 2                       # one retry, then it gives up
    assert logins == []


def test_authed_budget_network_then_404_then_success(no_sleep):
    """The case a single counter could not express.

    With `for attempt in (1, 2)` the blip consumed the only attempt, so the 404 that
    followed had no re-login left. Two independent counters spend one of each.
    """
    transport = Transport(
        urllib.error.URLError("connection reset"),
        (200, _envelope(code=CODE_NOT_AUTHORIZED, message="session invalidated")),
        (200, _envelope(data={"devices": []})),
    )
    api = _api(transport, logged_in=True)
    logins = _count_logins(api)

    assert asyncio.run(api.list_devices(1)) == []
    assert transport.count == 3                       # hard cap: 3 HTTP requests
    assert len(logins) == 1                           # hard cap: 1 re-login


def test_authed_400_raises_without_relogin(no_sleep):
    """A 400 is the server saying no; re-logging in cannot change its mind."""
    transport = Transport((400, _envelope(code=400, message="bad request", data=None)))
    api = _api(transport, logged_in=True)
    logins = _count_logins(api)

    with pytest.raises(NavienApiError) as caught:
        asyncio.run(api.list_devices(1))
    assert not isinstance(caught.value, NavienNetworkError)
    assert transport.count == 1
    assert logins == []
    assert "code 400" in str(caught.value)
    # F10: the verdict travels on the error, not only inside its message. The control path
    # reads this to stop re-sending a command the server has already ruled on.
    assert caught.value.code == CODE_BAD_REQUEST


def test_network_error_carries_no_code():
    """The other half of F10's contract. NavienNetworkError subclasses NavienApiError, so
    anything branching on `code` would catch it too if it inherited a code — and a request
    that never reached the server has no verdict to obey. `code is None` is what keeps the
    retryable case retryable."""
    transport = Transport(urllib.error.URLError("no route to host"))
    api = _api(transport, logged_in=True)

    with pytest.raises(NavienNetworkError) as caught:
        asyncio.run(api.list_devices(1))
    assert caught.value.code is None
    assert isinstance(caught.value, NavienApiError)     # still caught by existing handlers


def test_pairing_budget_covers_the_worst_case_authed_call(no_sleep):
    """M6. `pairing.LOGIN_TIMEOUT_S` bounds calls whose worst case this module defines, so
    the two have to be checked against each other rather than chosen apart.

    Measured, not asserted from the comment: script every response to fail the way the
    budget allows (network blip -> 404 -> 400) and count the HTTP requests and re-logins
    that actually happen, then price them at the 15 s socket timeout plus the recorded
    retry pause. At 25 s the bound was under half of this, so it fired on a healthy but
    slow login — and `wait_for` cannot cancel the executor thread it gives up on.
    """
    transport = Transport(
        urllib.error.URLError("blip"),
        (CODE_NOT_AUTHORIZED, _envelope(code=CODE_NOT_AUTHORIZED, message="gone")),
        (400, _envelope(code=400, message="bad request")),
    )
    api = _api(transport, logged_in=True)
    logins = _count_logins(api)

    with pytest.raises(NavienApiError):
        asyncio.run(api.list_devices(1))

    # 3 requests + 1 re-login, and a real re-login is itself two HTTP calls.
    assert transport.count == 3
    assert len(logins) == 1
    worst_case = (transport.count + 2 * len(logins)) * 15 + sum(no_sleep)
    assert worst_case == 76
    assert pairing.LOGIN_TIMEOUT_S >= worst_case


# --- login ownership: one session per account --------------------------------


class SessionTransport:
    """Answers by which token the request presented, the way the single session does.

    The scripted `Transport` answers by call index, and `_run` hands each request to a
    different executor thread, so which entry a request gets depends on the order those
    threads happen to arrive — not on the order the requests were issued. That made the
    concurrent-login test ordering-dependent in two directions: it failed on CI with two
    logins, and about once in a thousand runs locally by handing a caller's own retry the
    second scripted 404 and exhausting a re-login budget that was never meant to cover it.

    Both symptoms come from the same thing: a 404 answering a request that carried a token
    minted moments earlier. The cloud does not do that, and when it genuinely does, a
    second re-login is the correct response — so the fixture, not the dedup, was wrong.
    Here the only invalidated token is the one `_api(logged_in=True)` starts with, and
    every token a re-login mints is accepted, which makes the answer independent of arrival
    order. Verified over 6000 runs.

    `gate` additionally holds the first N requests inside the transport until all N have
    arrived, so a test about simultaneous callers gets simultaneous callers and its request
    count is pinned too.
    """

    STALE_TOKEN = "TOKEN-1"

    def __init__(self, *, gate=0):
        self.calls = []
        self._gate = threading.Barrier(gate, timeout=10) if gate else None
        self._lock = threading.Lock()
        self._arrived = 0

    def __call__(self, method, url, headers, body, allow_redirects):
        if self._gate is not None:
            with self._lock:
                self._arrived += 1
                hold = self._arrived <= self._gate.parties
            # A timeout here breaks the barrier and surfaces as an error rather than
            # hanging the suite, so a test that stops issuing the requests it promised
            # fails loudly.
            if hold:
                self._gate.wait()
        self.calls.append((method, url, body))
        if headers.get("Authorization") == self.STALE_TOKEN:
            return 200, _envelope(code=CODE_NOT_AUTHORIZED, message="session invalidated")
        return 200, _envelope(data={"devices": []})

    @property
    def count(self) -> int:
        return len(self.calls)


def test_concurrent_404s_cause_exactly_one_login():
    """Two devices bounced in the same instant must re-login once between them.

    On this account each login invalidates the previous session, so the second re-login
    would knock out the session the first one just established — the retry causing the
    condition it exists to repair. The generation captured before each request is what
    lets the loser of the race see that the work is already done.

    Both requests are gated into flight together because that is the premise: only a
    caller that had already snapshotted the pre-login generation can be the loser this
    dedup is for. Leaving it to the scheduler tested a different thing on CI.
    """
    transport = SessionTransport(gate=2)
    api = _api(transport, logged_in=True)
    logins = _count_logins(api)

    async def both():
        return await asyncio.gather(api.list_devices(1), api.list_devices(2))

    assert asyncio.run(both()) == [[], []]
    assert transport.count == 4                       # two 404s, then two retries
    assert len(logins) == 1


def test_a_request_issued_after_the_relogin_does_not_redo_it():
    """A caller that starts late rides the new session instead of replacing it.

    Its generation snapshot is taken *after* the login, so the generation check cannot tell
    it that one just happened — and it does not have to. It presents the token that login
    minted, the cloud accepts it, and there is no 404 to react to. This is the other half
    of the guarantee above: the dedup covers callers that raced, and callers that did not
    race need no covering.
    """
    transport = SessionTransport()
    api = _api(transport, logged_in=True)
    logins = _count_logins(api)

    async def one_then_the_other():
        return [await api.list_devices(1), await api.list_devices(2)]

    assert asyncio.run(one_then_the_other()) == [[], []]
    assert transport.count == 3            # bounced, retried on the new token, then clean
    assert len(logins) == 1


def test_login_if_stale_skips_when_generation_advanced():
    """A generation that moved on means somebody else already logged in for us."""
    api = _api(Transport((200, "")))
    logins = _count_logins(api)
    api._auth_gen = 3
    api._auth_at = time.monotonic()

    asyncio.run(api.login_if_stale(2))

    assert logins == []


def test_login_if_stale_remints_credentials_older_than_the_freshness_window():
    """Skipping on the generation alone would be wrong.

    `_login_locked` re-mints `self.aws` every time and `build_signed_ws_path` sets no
    `X-Amz-Expires`, so a reconnect that reused an old generation would presign from
    whatever temporary credentials happened to still be on the object.
    """
    api = _api(Transport((200, "")))
    logins = _count_logins(api)
    api._auth_gen = 3
    api._auth_at = time.monotonic() - (AWS_CREDS_FRESH_S + 1)

    asyncio.run(api.login_if_stale(2))

    assert len(logins) == 1


# --- logout: the session object is the only seam onto a running device -------


def test_authed_refuses_when_disabled():
    """Zero HTTP, not a failed request: the point is that a logged-out account stops
    talking to the cloud at all, and `transport.count` is what says so."""
    transport = Transport((200, _envelope(data={"devices": []})))
    api = _api(transport, logged_in=True)
    api.disabled = True

    with pytest.raises(NavienApiError) as caught:
        asyncio.run(api.list_devices(1))

    assert transport.count == 0
    assert "로그아웃" in str(caught.value)


def test_login_also_refuses_when_disabled():
    """`_ensure_mqtt` calls login directly, bypassing `_authed` entirely, and Phase 1's F2
    fix makes it run every cycle whether or not the REST read failed. Gating only `_authed`
    would therefore leave a logged-out device attempting one login per poll interval."""
    transport = Transport((200, _LOGIN_HTML))
    api = _api(transport)
    api.disabled = True

    with pytest.raises(NavienApiError):
        asyncio.run(api.login())
    with pytest.raises(NavienApiError):
        asyncio.run(api.login_if_stale(api.auth_gen))

    assert transport.count == 0


# --- the settings page shares the app's one session --------------------------


class LoginTransport:
    """Answers the two-step login by password: the real one works, anything else gets the
    login page's rejection popup — which is how `_form_login` recognises a bad account."""

    def __init__(self, good_password: str):
        self.good = good_password
        self.calls = []

    def __call__(self, method, url, headers, body, allow_redirects):
        self.calls.append(url)
        if url.endswith("/member/login"):
            password = urllib.parse.parse_qs(body.decode())["password"][0]
            if password != self.good:
                return 200, '<html><body><div id="loginFailPopup"></div></body></html>'
            return 200, _LOGIN_HTML
        if url.endswith("/users/secured-sign-in"):
            return 200, _envelope(data={"userInfo": {"userSeq": 77},
                                        "home": [{"homeSeq": 5, "homeName": "우리집"}]})
        return 200, _envelope(data={"devices": []})


def _settings_app(homey, transport, monkeypatch):
    """A real `NavienApp` wired into `homey`, with its NavienApi built over `transport`.

    The app object is the thing under test here: `_client` is where the shared session's
    credentials are replaced in place, before the login that validates them is attempted.
    """
    import app as app_module

    monkeypatch.setattr(
        app_module, "NavienApi",
        lambda **kwargs: NavienApi(transport=transport, **kwargs),
    )
    app = app_module.NavienApp(homey=homey)
    homey.app = app
    return app


def test_save_credentials_restores_the_shared_session_on_a_wrong_password(
        make_homey, monkeypatch):
    """One typo used to log every running device out until the app was restarted.

    Routing validation through the shared session (F4) is what makes that possible: `reauth`
    points the shared client at the typed password and clears its token *before* trying it,
    and devices cache that object forever. So the routing and the restore have to arrive
    together — this asserts both halves, and that the settings were left alone.
    """
    import api as settings_api

    async def scenario():
        homey = make_homey(settings={SETTING_USERNAME: "me@example.com",
                                     SETTING_PASSWORD: "right-pw",
                                     SETTING_HOME_SEQ: "5"})
        app = _settings_app(homey, LoginTransport("right-pw"), monkeypatch)
        await app.on_init()
        shared = await app.shared_api()
        assert (shared.username, shared.password) == ("me@example.com", "right-pw")

        result = await settings_api.save_credentials(
            homey, body={"username": "me@example.com", "password": "wrong-pw"})

        assert result["ok"] is False
        assert app._api is shared                      # updated in place, never replaced
        assert (shared.username, shared.password) == ("me@example.com", "right-pw")
        assert shared.access_token                     # and logged back in, not just reset
        assert homey.settings.values[SETTING_PASSWORD] == "right-pw"
        assert any("credentials restored after failed save" in line for line in app.logs)

    asyncio.run(scenario())


def test_save_credentials_logs_when_the_restore_itself_fails(make_homey, monkeypatch):
    """A restore that fails leaves every device logged out, so it cannot be silent.

    Staged with no saved account at all — `shared_api` raises on empty settings — which is
    the same shape of failure `pairing._restore_shared` swallows today.
    """
    import api as settings_api

    async def scenario():
        homey = make_homey(settings={})
        app = _settings_app(homey, LoginTransport("right-pw"), monkeypatch)
        await app.on_init()

        result = await settings_api.save_credentials(
            homey, body={"username": "me@example.com", "password": "wrong-pw"})

        assert result["ok"] is False
        assert any("credentials restore FAILED" in line for line in app.logs)

    asyncio.run(scenario())


# --- B1: logging out must not be a one-way door ------------------------------

_AIRONE_STORE = {STORE_DEVICE_SEQ: 12345, STORE_DEVICE_ID: "AIR-XYZ",
                 STORE_PHYSICAL_ID: "RC-77", STORE_MODEL_CODE: 1024}


def test_logout_then_relogin_revives_a_running_device(make_homey, monkeypatch):
    """B1. 계정 삭제 → the same account re-entered → every device polling again.

    The round trip is the test, because each leg passed on its own. `logout` dropped
    `self._api` *as well as* disabling it, so the next login built a brand-new NavienApi
    while every device went on holding the disabled one — and nothing anywhere ever cleared
    `disabled`. Devices re-read the session only in `_acquire_api`, which runs once, in
    `_run`, and `_run`'s loop swallows everything short of CancelledError, so the poll task
    never dies and never re-enters it. Saving correct credentials therefore reported success
    while every tile stayed greyed out until the app was restarted.

    Driven through the real `NavienApp` and a real `AironeDevice_`, because the defect lives
    exactly in the seam between them: the app's one object and the device's one cache of it.
    """
    import api as settings_api
    from navien_lib import compat
    from navien_lib.airone import device as airone_device

    async def scenario():
        homey = make_homey(settings={SETTING_USERNAME: "me@example.com",
                                     SETTING_PASSWORD: "right-pw",
                                     SETTING_HOME_SEQ: "5"})
        transport = LoginTransport("right-pw")
        app = _settings_app(homey, transport, monkeypatch)
        await app.on_init()

        dev = airone_device.AironeDevice_(
            homey=homey, store=_AIRONE_STORE, capabilities=["onoff"], name="거실 에어원")
        await dev.on_init()
        await dev._teardown()          # park the loop; this test drives the polls by hand
        dev._closing = False
        # The one and only time a device fetches the session (`_acquire_api`).
        dev._api = await compat.shared_api(homey)
        assert dev._api is app._api

        await settings_api.clear_credentials(homey)
        before = len(transport.calls)

        await dev._poll_once()
        await dev._poll_once()

        assert len(transport.calls) == before      # logged out means zero HTTP, not 401s
        assert dev.available is False
        assert dev.unavailable_reason == "나비엔 서버에 연결할 수 없습니다"

        result = await settings_api.save_credentials(
            homey, body={"username": "me@example.com", "password": "right-pw"})
        assert result["ok"] is True

        await dev._poll_once()

        assert dev.available is True
        # Still the one session, re-enabled in place — not a second one the device cannot
        # see. Both halves matter: one session per account, and a device that recovers.
        assert dev._api is app._api
        assert dev._api.disabled is False

    asyncio.run(scenario())


def test_reauth_leaves_a_logged_out_session_logged_out_on_a_bad_password(
        make_homey, monkeypatch):
    """The fence on the re-enable. Clearing `disabled` for the attempt is what lets a
    logged-out account come back, but clearing it *permanently* would let every device
    resume polling with credentials the server just rejected — a login storm on a
    one-session account, behind an error message telling the user the save failed."""

    async def scenario():
        homey = make_homey(settings={SETTING_USERNAME: "me@example.com",
                                     SETTING_PASSWORD: "right-pw"})
        app = _settings_app(homey, LoginTransport("right-pw"), monkeypatch)
        await app.on_init()
        shared = await app.shared_api()
        await app.logout()
        assert shared.disabled is True

        with pytest.raises(NavienAuthError):
            await app.reauth("me@example.com", "wrong-pw")

        assert shared.disabled is True
        assert app._api is shared

    asyncio.run(scenario())


# --- B2: "연결 확인" has to actually check the connection ---------------------


def test_check_connection_detects_a_session_that_no_longer_authenticates(
        make_homey, monkeypatch):
    """B2. The button reported success without asking the server anything.

    `shared_api` only logs in when `access_token` is falsy, so once any device had logged in
    it handed back the live object untouched; `home_seqs()` is a local read off that object
    and `_detect_devices` swallowed its own exception, so `ok: True` came back
    unconditionally and the `except NavienAuthError` branch was unreachable. It did real
    work only in the narrow window before the first device login — i.e. it was
    non-deterministically wrong.

    Staged as the failure a user actually hits: the password was changed in the Navien app,
    so the saved one no longer authenticates while the old token is still on the object.
    """
    import api as settings_api

    async def scenario():
        homey = make_homey(settings={SETTING_USERNAME: "me@example.com",
                                     SETTING_PASSWORD: "right-pw",
                                     SETTING_HOME_SEQ: "5"})
        transport = LoginTransport("right-pw")
        app = _settings_app(homey, transport, monkeypatch)
        await app.on_init()
        shared = await app.shared_api()
        assert shared.access_token              # a device has already logged in

        transport.good = "changed-in-the-navien-app"
        result = await settings_api.check_connection(homey)

        assert result["ok"] is False
        assert "아이디 또는 비밀번호" in result["error"]

    asyncio.run(scenario())


def test_check_connection_fails_when_the_device_list_cannot_be_read(
        make_homey, monkeypatch, no_sleep):
    """The second half of B2, isolated: here the login succeeds, so only the device-list
    read can tell the user anything. Swallowing its failure made an unreachable server
    indistinguishable from an account with no appliances on it — both rendered as
    "✓ 연결 정상"."""
    import api as settings_api

    class DeviceListDown(LoginTransport):
        def __call__(self, method, url, headers, body, allow_redirects):
            if "/devices" in url:
                raise urllib.error.URLError("no route to host")
            return super().__call__(method, url, headers, body, allow_redirects)

    async def scenario():
        homey = make_homey(settings={SETTING_USERNAME: "me@example.com",
                                     SETTING_PASSWORD: "right-pw",
                                     SETTING_HOME_SEQ: "5"})
        app = _settings_app(homey, DeviceListDown("right-pw"), monkeypatch)
        await app.on_init()

        result = await settings_api.check_connection(homey)

        assert result["ok"] is False
        assert "연결에 실패했습니다" in result["error"]

    asyncio.run(scenario())


# --- B7: logout stops every session we own, including the degraded-mode one --


def test_app_logout_disables_the_cached_fallback_session(make_homey, monkeypatch):
    """B7. `compat._FALLBACK_API` is a session on the same account, cached and handed to
    devices exactly like the app-level one, so a logout that skips it leaves a degraded
    runtime polling a deleted account. Asserted dead code — that is what `_FALLBACK_WARNED`
    is instrumenting — so this is a consistency gap rather than a live defect, but the
    invariant it belongs to has no exceptions."""
    from navien_lib import compat

    async def scenario():
        homey = make_homey()
        fallback = NavienApi(username="me@example.com", password="pw",
                             transport=LoginTransport("pw"))
        monkeypatch.setattr(compat, "_FALLBACK_API", fallback)

        await compat.app_logout(homey)

        assert fallback.disabled is True

    asyncio.run(scenario())
