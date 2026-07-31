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
            api = self._api
            if api is None or api.username != username or api.password != password:
                api = NavienApi(username=username, password=password, log=self.log)
                self._api = api
            if not api.access_token:
                await api.login()
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
