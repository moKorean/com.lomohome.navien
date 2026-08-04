"""A fake `homey` package, installed into `sys.modules` before any test is collected.

`import homey` genuinely fails in this checkout — `python_packages/*/.venv/.../site-packages`
carries only `certifi` and `paho-mqtt` — so without this file none of the device modules
(`navien_lib/airone`, `navien_lib/mate`, `navien_lib/airmonitor`) can even be imported, and
the device-lifecycle tests cannot be collected.

The surface below was enumerated by reading those modules, not guessed; every stub names
the call site that needs it. Strictness is the point: an attribute nobody stubbed raises
AttributeError instead of returning a mock that swallows the call. A permissive fake would
let a test pass against a member the real SDK does not have, which is exactly the risk this
harness exists to bound.

Both calling contracts are supported on purpose. `compat.resolve` awaits whatever comes
back if it is awaitable (compat.py:20-24), and `compat.flow_card` / `register_run_listener`
try the snake_case and camelCase spellings in turn (compat.py:112-135), because the SDK's
Python surface is not pinned. compat.py:1-9 names getting this wrong as the app's silent
failure mode — an un-awaited `settings.set()` coroutine looks like a successful write and
stores nothing — so the fake lets each test pick the contract it stands on rather than
baking one in.

Nothing here touches `test_airone.py` / `test_mate.py`: they import `navien_lib.navien.*`
only, which never imports `homey`.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _Strict:
    """Base whose unstubbed attributes raise, with a message saying what to do.

    Plain classes already raise AttributeError for a missing member; this only makes the
    message say *why* the fake is empty there, so a test that drifts onto an unmodelled
    part of the SDK fails loudly instead of quietly.
    """

    def __getattr__(self, name):
        raise AttributeError(
            f"{type(self).__name__} has no {name!r}. If that is part of the homey SDK "
            f"surface, add it to tests/conftest.py deliberately, after checking the real "
            f"SDK has it — the fake stubs only what navien_lib actually calls. If it "
            f"belongs to the device class itself, nothing ever assigned it."
        )


def _maybe_async(value, awaitable: bool):
    """Return `value`, or a coroutine yielding it, per the contract under test."""
    if not awaitable:
        return value

    async def _coro():
        return value

    return _coro()


class FakeSettings(_Strict):
    """`homey.settings` — compat.setting_get/set/unset (compat.py:29, :36, :46-48)."""

    def __init__(self, values=None, *, awaitable=False, has_unset=True):
        self.values = dict(values or {})
        self.writes = []
        self._awaitable = awaitable
        self._has_unset = has_unset

    def get(self, key):
        return _maybe_async(self.values.get(key), self._awaitable)

    def set(self, key, value):
        self.values[key] = value
        self.writes.append((key, value))
        return _maybe_async(None, self._awaitable)

    def unset(self, key):
        # Not every build exposes unset(); compat.setting_unset falls back to set(key, "")
        # when it is missing, so the fake can model a build without it.
        if not self._has_unset:
            raise AttributeError("unset")
        self.values.pop(key, None)
        return _maybe_async(None, self._awaitable)


class FakeI18n(_Strict):
    """`homey.i18n` — compat.language tries get_language() then getLanguage() (:54-56)."""

    def __init__(self, language="ko", *, spelling="snake", awaitable=False):
        self.language = language
        self._awaitable = awaitable
        if spelling in ("snake", "both"):
            self.get_language = self._get_language
        if spelling in ("camel", "both"):
            self.getLanguage = self._get_language  # noqa: N815 — the SDK's own spelling

    def _get_language(self):
        return _maybe_async(self.language, self._awaitable)


class FakeApp(_Strict):
    """`homey.app` — compat.shared_api (:72-75) / reauth_shared_api (:99-102), and the
    `log` both of them reach for when they fall back (:82, :90, :108)."""

    def __init__(self, api=None, *, awaitable=False, expose_shared_api=True,
                 expose_reauth=True):
        self.api = api
        self.logs = []
        self.reauths = []
        self._awaitable = awaitable
        # Absent rather than present-and-None: compat uses getattr(app, "shared_api", None)
        # to decide whether the shared session exists at all, so the fallback branch is
        # only reachable when the attribute is genuinely missing.
        if expose_shared_api:
            self.shared_api = self._shared_api
        if expose_reauth:
            self.reauth = self._reauth

    def log(self, message):
        self.logs.append(str(message))

    def _shared_api(self):
        return _maybe_async(self.api, self._awaitable)

    def _reauth(self, username, password):
        self.reauths.append((username, password))
        return _maybe_async(None, self._awaitable)


class FakeFlowCard(_Strict):
    """One Flow card — compat.register_run_listener tries both spellings (:129-133)."""

    def __init__(self, card_id, *, spelling="snake"):
        self.card_id = card_id
        self.listener = None
        if spelling in ("snake", "both"):
            self.register_run_listener = self._register
        if spelling in ("camel", "both"):
            self.registerRunListener = self._register  # noqa: N815 — the SDK's spelling

    def _register(self, fn):
        self.listener = fn


class FakeFlow(_Strict):
    """`homey.flow` — compat.flow_card tries get_*_card then get*Card (:118-125)."""

    def __init__(self, *, spelling="snake"):
        self.cards = {}
        self._spelling = spelling
        if spelling in ("snake", "both"):
            self.get_action_card = self._action
            self.get_condition_card = self._condition
        if spelling in ("camel", "both"):
            self.getActionCard = self._action        # noqa: N815 — the SDK's spelling
            self.getConditionCard = self._condition  # noqa: N815 — the SDK's spelling

    def _card(self, kind, card_id):
        key = (kind, card_id)
        if key not in self.cards:
            self.cards[key] = FakeFlowCard(card_id, spelling=self._spelling)
        return self.cards[key]

    def _action(self, card_id):
        return self._card("action", card_id)

    def _condition(self, card_id):
        return self._card("condition", card_id)


class FakeHomey(_Strict):
    """The `self.homey` every device/driver is handed."""

    def __init__(self, *, settings=None, i18n=None, app=None, flow=None, language=None):
        self.settings = settings if settings is not None else FakeSettings()
        self.i18n = i18n if i18n is not None else FakeI18n()
        self.app = app if app is not None else FakeApp()
        self.flow = flow if flow is not None else FakeFlow()
        # compat.language's last resort (:56). Left unset unless a test asks for it, so
        # the accessor chain above is what actually gets exercised.
        if language is not None:
            self.language = language


class Device(_Strict):
    """Stand-in for `homey.device.Device`, recording what the device layer did.

    Members, and the call sites that need them:
      sync  — get_store (airone/device.py:96), get_capabilities (:139, :581),
              register_capability_listener (:140), get_name (:107), log (:107),
              get_capability_value (:584)
      async — set_available (:615), set_unavailable (:621), set_capability_value (:585),
              set_capability_options (:502)
      attr  — homey (:101, :103, :232)

    The constructor signature is ours, not the SDK's — the real runtime builds these
    objects itself, so tests need *some* way in. Everything else mirrors a call the app
    genuinely makes.
    """

    def __init__(self, *, homey=None, store=None, capabilities=(), name="테스트 기기",
                 capability_values=None):
        self.homey = homey if homey is not None else FakeHomey()
        self.logs = []
        self.listeners = {}
        self.capability_options = {}
        self.availability = []          # ordered ("available"|"unavailable", reason)
        self.available = None
        self.unavailable_reason = None
        self._store = dict(store or {})
        self._capabilities = list(capabilities)
        self._name = name
        self._values = dict(capability_values or {})
        self.added_capabilities = []    # in call order, for the powerCtrl migration

    # --- sync ---------------------------------------------------------------

    def get_store(self) -> dict:
        return dict(self._store)

    def get_capabilities(self) -> list:
        return list(self._capabilities)

    def register_capability_listener(self, capability, listener) -> None:
        self.listeners[capability] = listener

    def get_name(self) -> str:
        return self._name

    def log(self, *parts) -> None:
        self.logs.append(" ".join(str(p) for p in parts))

    def get_capability_value(self, capability):
        return self._values.get(capability)

    # --- async --------------------------------------------------------------

    async def set_available(self) -> None:
        self.available = True
        self.unavailable_reason = None
        self.availability.append(("available", None))

    async def set_unavailable(self, reason=None) -> None:
        self.available = False
        self.unavailable_reason = reason
        self.availability.append(("unavailable", reason))

    async def set_capability_value(self, capability, value) -> None:
        if capability not in self._capabilities:
            # The real SDK rejects a capability the device does not have; the app relies
            # on filtering these out itself (_set checks get_capabilities first).
            raise ValueError(f"device has no capability {capability!r}")
        self._values[capability] = value

    async def set_capability_options(self, capability, options) -> None:
        if capability not in self._capabilities:
            raise ValueError(f"device has no capability {capability!r}")
        self.capability_options[capability] = dict(options)

    async def add_capability(self, capability) -> None:
        """Present only when a test opts in, because we could not confirm it is real.

        SDK3's JS Device has `addCapability`, but the Python runtime's documented Device API
        does not list an equivalent, and there is no local stub to check against. The app
        therefore probes with `getattr` and logs either way, and this fake is deleted from
        instances that want the absent branch (`no_add_capability`) so both halves of that
        probe are covered rather than assumed.
        """
        if capability not in self._capabilities:
            self._capabilities.append(capability)
        self.added_capabilities.append(capability)


class App(_Strict):
    """Stand-in for `homey.app.App`, the base root app.py subclasses (app.py:20, :32).

    Only `log` and `homey` are stubbed, because those are the only two members `NavienApp`
    reaches for on its base. Having it here means the shared-session tests exercise the real
    `NavienApp._client` — where the in-place credential update that F4's restore exists to
    undo actually lives — rather than a re-implementation of it.
    """

    def __init__(self, *, homey=None):
        self.homey = homey if homey is not None else FakeHomey()
        self.logs = []

    def log(self, *parts) -> None:
        self.logs.append(" ".join(str(p) for p in parts))


class Driver(_Strict):
    """Stand-in for `homey.driver.Driver` — log (airone/driver.py:23) and homey (:53)."""

    def __init__(self, *, homey=None, name="테스트 드라이버"):
        self.homey = homey if homey is not None else FakeHomey()
        self.logs = []
        self._name = name

    def log(self, *parts) -> None:
        self.logs.append(" ".join(str(p) for p in parts))

    def get_name(self) -> str:
        return self._name


def _install_fake_homey() -> None:
    homey_module = types.ModuleType("homey")
    device_module = types.ModuleType("homey.device")
    driver_module = types.ModuleType("homey.driver")
    app_module = types.ModuleType("homey.app")
    device_module.Device = Device
    driver_module.Driver = Driver
    app_module.App = App
    homey_module.device = device_module
    homey_module.driver = driver_module
    homey_module.app = app_module
    sys.modules["homey"] = homey_module
    sys.modules["homey.device"] = device_module
    sys.modules["homey.driver"] = driver_module
    sys.modules["homey.app"] = app_module


try:  # a real SDK, wherever one exists, always wins over the fake
    import homey  # noqa: F401
except ImportError:
    _install_fake_homey()


@pytest.fixture
def make_homey():
    """Factory for a `self.homey`; every part of the surface is selectable.

    `awaitable=True` makes settings/i18n/app return coroutines instead of values, which is
    the half of compat.resolve's contract that fails silently when it is wrong.
    """

    def _make(*, api=None, settings=None, awaitable=False, language="ko",
              i18n_spelling="snake", flow_spelling="snake", expose_shared_api=True,
              homey_language=None):
        return FakeHomey(
            settings=FakeSettings(settings, awaitable=awaitable),
            i18n=FakeI18n(language, spelling=i18n_spelling, awaitable=awaitable),
            app=FakeApp(api, awaitable=awaitable, expose_shared_api=expose_shared_api),
            flow=FakeFlow(spelling=flow_spelling),
            language=homey_language,
        )

    return _make
