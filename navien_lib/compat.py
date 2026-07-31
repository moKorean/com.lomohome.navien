"""Homey Python SDK accessors that tolerate either calling contract.

The SDK's Python surface is only partly documented, so there is no ground truth for
whether settings/i18n return values or coroutines. Rather than betting on one, await
whatever comes back if it is awaitable. Getting this wrong is silent: an un-awaited
`settings.set()` coroutine looks like a successful write and stores nothing.

Copied, near-verbatim, from com.lomohome.localthings — this layer is vendor-neutral.
"""

import inspect


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
