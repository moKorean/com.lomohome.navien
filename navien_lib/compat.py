"""Homey Python SDK accessors that tolerate either calling contract.

The SDK's Python surface is only partly documented, so there is no ground truth for
whether settings/i18n return values or coroutines. Rather than betting on one, await
whatever comes back if it is awaitable. Getting this wrong is silent: an un-awaited
`settings.set()` coroutine looks like a successful write and stores nothing.

Copied, near-verbatim, from com.lomohome.localthings — this layer is vendor-neutral.
"""

import inspect

# Gate 0 Q1 instrumentation: whether `shared_api`'s private-session fallback has ever
# fired on a real runtime. It is the one branch that silently opens a second account
# session, and the whole one-session-per-account design assumes it is dead code — so the
# warning fires once per app start, loudly, and nothing else changes.
_FALLBACK_WARNED = False

# The one fallback session, cached. Before this, every `shared_api` call on a runtime
# without `homey.app` built a *new* NavienApi, so N devices × every retry opened N sessions
# against an account the cloud allows one session on — each new login bouncing the previous
# device with 404s. Caching makes the degraded mode cost one session instead, i.e. as close
# to the shared-session design as this branch can get. Kept module-level, like the warning,
# because there is no app object here to hang it on.
_FALLBACK_API = None


async def resolve(value):
    """Return `value`, awaiting it first if it is awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


async def setting_get(homey, key: str, default: str = "") -> str:
    try:
        value = await resolve(homey.settings.get(key))
    except Exception:
        return default
    return default if value is None else value


async def setting_set(homey, key: str, value) -> None:
    await resolve(homey.settings.set(key, value))


async def setting_unset(homey, key: str) -> None:
    """Remove a setting, falling back to an empty value.

    Not every build exposes unset(); an empty string reads the same to every
    consumer in this app.
    """
    try:
        await resolve(homey.settings.unset(key))
    except Exception:
        await resolve(homey.settings.set(key, ""))


async def language(homey, default: str = "en") -> str:
    """Two-letter UI language, or `default` if it can't be determined."""
    for get in (
        lambda: homey.i18n.get_language(),
        lambda: homey.i18n.getLanguage(),
        lambda: homey.language,
    ):
        try:
            value = await resolve(get())
        except Exception:
            continue
        if isinstance(value, str) and value:
            return value[:2].lower()
    return default


async def shared_api(homey):
    """Return the app-wide shared NavienApi (one session per account), logging in if
    needed. Falls back to a private session if the app object can't be reached, so a
    device still works on a runtime that doesn't expose `homey.app`."""
    global _FALLBACK_WARNED, _FALLBACK_API
    app = getattr(homey, "app", None)
    getter = getattr(app, "shared_api", None) if app is not None else None
    if getter is not None:
        return await resolve(getter())

    from .const import SETTING_PASSWORD, SETTING_USERNAME
    from .navien.api import NavienApi

    if not _FALLBACK_WARNED:
        _FALLBACK_WARNED = True
        log = getattr(app, "log", print) if app is not None else print
        log("navien: WARNING shared_api fallback — homey.app exposes no shared_api, so "
            "this device is opening its OWN account session. Navien allows one session "
            "per account, so every other device will be bounced with 404s.")

    username = await setting_get(homey, SETTING_USERNAME)
    password = await setting_get(homey, SETTING_PASSWORD)
    api = _FALLBACK_API
    if api is None:
        api = _FALLBACK_API = NavienApi(
            username=username, password=password,
            log=getattr(app, "log", print) if app is not None else print,
        )
    elif api.username != username or api.password != password:
        # Updated in place rather than replaced, exactly as NavienApp._client does it, so a
        # device still holding this object picks up a repair without a re-init.
        api.username = username
        api.password = password
        api.access_token = ""        # force a fresh login with the new credentials
        api.aws = None
    # The other half of `app_logout`'s disable, and it has to live here: a runtime that
    # reaches this branch has no `homey.app`, hence no `reauth` to re-enable the session
    # the way NavienApp does. Saved credentials being present again is the only re-login
    # signal this branch gets — and `clear_credentials` unsets them immediately after
    # logging out, so a cleared account stays refused.
    if username and password:
        api.disabled = False
    if not api.access_token:
        await api.login()
    return api


async def reauth_shared_api(homey, username: str, password: str):
    """Validate credentials by pointing the app-wide shared session at them and logging
    in; raises on failure. Falls back to a throwaway login if the app can't be reached.

    Returns the validated session so the caller can read `home_seqs()` off it. Callers that
    only care about success (pairing) can keep ignoring it.
    """
    app = getattr(homey, "app", None)
    fn = getattr(app, "reauth", None) if app is not None else None
    if fn is not None:
        return await resolve(fn(username, password))

    from .navien.api import NavienApi

    api = NavienApi(username=username, password=password,
                    log=getattr(app, "log", print) if app is not None else print)
    await api.login()
    return api


async def app_logout(homey) -> None:
    """Disable the app-wide shared session, so devices holding it stop making requests.

    A no-op where `homey.app` exposes no `logout` — the same tolerance every accessor here
    applies, and the shared-session design is already absent on such a runtime.
    """
    app = getattr(homey, "app", None)
    fn = getattr(app, "logout", None) if app is not None else None
    if fn is not None:
        await resolve(fn())
    # B7. `_FALLBACK_API` is a session on the same account, cached and handed to devices
    # exactly like the app-level one, so a logout that skips it leaves the degraded runtime
    # still polling a deleted account. This is asserted dead code — that is what the
    # `_FALLBACK_WARNED` instrumentation above is for — so this closes a consistency gap
    # rather than a live defect, but the invariant is "logout stops every session we own".
    if _FALLBACK_API is not None:
        _FALLBACK_API.disabled = True


def flow_card(homey, kind: str, card_id: str):
    """Fetch a flow card, tolerating either the snake_case or camelCase SDK spelling.

    `kind` is "action" or "condition". As with settings/i18n, the Python surface isn't
    pinned, so we try both method names rather than betting on one.
    """
    getters = {
        "action": ("get_action_card", "getActionCard"),
        "condition": ("get_condition_card", "getConditionCard"),
    }[kind]
    for name in getters:
        fn = getattr(homey.flow, name, None)
        if fn is not None:
            return fn(card_id)
    raise AttributeError(f"Homey flow has no {kind}-card getter")


def register_run_listener(card, fn) -> None:
    for name in ("register_run_listener", "registerRunListener"):
        reg = getattr(card, name, None)
        if reg is not None:
            reg(fn)
            return
    raise AttributeError("flow card has no run-listener registrar")


async def ui_language(homey, default: str = "en") -> str:
    """The language to write user-facing messages in.

    Prefers what a webview reported, because Homey's Python i18n resolves the *app's*
    language rather than the user's and returns 'en' regardless (docs/PORTING.md).
    Falls back to that accessor anyway, in case a future firmware fixes it.
    """
    from .const import SETTING_UI_LANGUAGE

    reported = await setting_get(homey, SETTING_UI_LANGUAGE)
    if reported:
        return reported[:2].lower()
    return await language(homey, default)


async def remember_ui_language(homey, value: str) -> None:
    """Store a language a webview resolved, if it looks like one."""
    from .const import SETTING_UI_LANGUAGE

    code = str(value or "")[:2].lower()
    if not code.isalpha() or len(code) != 2:
        return
    if await setting_get(homey, SETTING_UI_LANGUAGE) == code:
        return
    await setting_set(homey, SETTING_UI_LANGUAGE, code)
