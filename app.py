"""Navien Smart — control Navien AirOne appliances from Homey.

Copyright 2026, Geunwon Mo (mokorean@gmail.com)

A Homey port of the navien_smart_ha Home Assistant integration
(https://github.com/ripe-avocado/navien_smart_ha) by Eui Young Jung, used under the
MIT License — the original notice is preserved in NOTICE. This app talks to the same
Navien cloud the official app uses: REST for control, AWS IoT MQTT for realtime state.
See docs/PORTING.md for the design.
"""

import asyncio
import json
import sys
from pathlib import Path

# The Homey runner may not put the app directory on sys.path.
sys.path.insert(0, str(Path(__file__).parent))

from homey import app as homey_app

from navien_lib import compat, selfcheck
from navien_lib.const import (
    SETTING_PAIR_ENV,
    SETTING_PASSWORD,
    SETTING_UI_LANGUAGE,
    SETTING_USERNAME,
)
from navien_lib.navien.api import NavienApi


class NavienApp(homey_app.App):
    async def on_init(self) -> None:
        self._api = None
        self._api_lock = asyncio.Lock()
        selfcheck.run(self.log)
        await self._seed_ui_language()
        self.log("Navien Smart app is running...")

    def _client(self, username: str, password: str) -> NavienApi:
        """The one shared NavienApi object, kept *stable* — its credentials are updated
        in place rather than replaced, so devices that already hold a reference pick up a
        repair (password change) without re-init."""
        if self._api is None:
            self._api = NavienApi(username=username, password=password, log=self.log)
        elif self._api.username != username or self._api.password != password:
            self._api.username = username
            self._api.password = password
            self._api.access_token = ""   # force a fresh login with the new credentials
            self._api.aws = None
        return self._api

    async def shared_api(self) -> NavienApi:
        """The single Navien session shared by every device on this account.

        Navien allows only one session per account, so each device holding its own
        login used to bounce the others with 403s (and the phone app bounces all of
        them). One app-level client keeps one session; the lock serialises the first
        login so devices starting together don't race into a login storm.
        """
        async with self._api_lock:
            username = await compat.setting_get(self.homey, SETTING_USERNAME)
            password = await compat.setting_get(self.homey, SETTING_PASSWORD)
            if not username or not password:
                raise RuntimeError("먼저 앱 설정에서 나비엔 계정으로 로그인하세요.")
            api = self._client(username, password)
            if not api.access_token:
                await api.login()
            return api

    async def logout(self) -> NavienApi | None:
        """Stop every running device before the account is removed.

        Dropping `self._api` does nothing on its own: each device caches the object it got
        from `_acquire_api` and never asks for it again, so it would keep polling a live
        session for an account the user just deleted. There is no device registry to reach
        through either — `homey` exposes no get_devices/get_driver here — so the object the
        devices already hold *is* the seam, and flipping `disabled` on it is what actually
        stops their traffic.

        `self._api` is deliberately *kept*. Clearing it as well used to make the logout
        permanent: `_client` would then build a brand-new NavienApi on the next login while
        every device went on holding the disabled one, and nothing ever cleared `disabled`.
        Devices only re-enter `_run` (the one place `_acquire_api` is called) if their poll
        task dies, and that loop catches everything short of CancelledError — so they never
        re-fetched the session either. Re-entering correct credentials appeared to succeed
        and every device stayed dead until the app was restarted. Keeping the one object and
        letting `reauth` re-enable it is what makes the recovery real, and it is also the
        only shape that preserves one session per account.

        Returns the disabled client so a caller can assert on it.
        """
        async with self._api_lock:
            api = self._api
            if api is not None:
                api.disabled = True
                self.log("navien: shared session disabled (logged out)")
            return api

    async def reauth(self, username: str, password: str) -> NavienApi:
        """Point the shared session at new credentials and log in to validate them.

        Used by repair and by the settings page: it updates the one shared client in place
        (so running devices recover on their next request) and raises if the credentials
        are wrong — the caller only writes them to settings once this succeeds.

        Because the update is in place and happens *before* `login()` is attempted, a
        rejected password leaves the shared session holding it; every caller must restore
        the saved account on failure (pairing._restore_shared, api._restore_shared).

        This is also where a logged-out session comes back to life, and it has to be here
        rather than in `_client`: after "계정 삭제" the user typically re-enters *the same*
        account, so `_client`'s `!=` comparison never fires and a re-enable hung off it
        would never run. `disabled` is cleared for the attempt and put back if the attempt
        fails, so a wrong password leaves a logged-out account logged out instead of
        letting every device resume polling with credentials the server just rejected.

        Returns the shared client so the caller can read `home_seqs()` off the session it
        just validated instead of opening a second one.
        """
        async with self._api_lock:
            api = self._client(username, password)
            was_disabled = api.disabled
            api.disabled = False
            try:
                await api.login()
            except Exception:
                api.disabled = was_disabled
                raise
            return api

    async def _seed_ui_language(self) -> None:
        """Recover the UI language reported by an earlier pairing session if unset.

        Homey's Python i18n reports the app's language, not the user's, so a webview
        has to tell us; the last one that resolved it left it in the stored pairing
        report. Without this, the first message after an upgrade would be English.
        """
        if await compat.setting_get(self.homey, SETTING_UI_LANGUAGE):
            return
        raw = await compat.setting_get(self.homey, SETTING_PAIR_ENV)
        if not raw:
            return
        try:
            reported = json.loads(raw).get("resolved")
        except Exception:
            return
        if reported:
            await compat.remember_ui_language(self.homey, reported)
            self.log(f"UI language recovered from a previous session: {reported}")


homey_export = NavienApp
