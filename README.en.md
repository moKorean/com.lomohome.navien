# Navien Smart Community (Homey)

A Homey app that controls Kyungdong **Navien AirOne** (ventilation / dehumidify /
air-purify) and **Navien sleep mats**. It talks to the same Navien Smart cloud the
official app uses — REST for control, AWS IoT MQTT for realtime state. This is an
**unofficial, community-built app and is not affiliated with Navien.**

> 한국어 README는 [`README.md`](./README.md)를 참고하세요.

## Credits · License

This app is a Homey port of the Home Assistant integration **navien_smart_ha**.

- **Original author:** Eui Young Jung
- **Original project:** https://github.com/ripe-avocado/navien_smart_ha
- **Original license:** MIT

The original project is used under the MIT license; the full copyright and permission
notice is preserved in [`NOTICE`](./NOTICE). All of this app's protocol knowledge (the
login flow, MQTT transport, and the AirOne/mat value tables) comes from the original
project. See [`docs/PORTING.md`](./docs/PORTING.md) for the design.

The Homey port itself is © 2026 Geunwon Mo.

## Features

- **AirOne control** — power, operating mode (auto, ventilate, ventilate + dehumidify,
  dehumidify, purify, cooking, sleep, bypass), fan speed (auto, saver, low, high, turbo),
  and a server-checked desired humidity in Dehumidify mode on the base fan (it's automatic
  with turbo/saver). The running-state sensor also shows "Auto drying" after Dehumidify.
- **Air-quality sensors** — PM1.0, PM2.5, PM10, CO₂, TVOC (ppb), radon, temperature,
  humidity, an overall air-quality score and filter usage. TVOC and radon also carry a
  "good / bad" grade label. Every reading is parsed as a number, so it graphs in Insights.
- **AirMonitor** — paired as its own sensor device.
- **Sleep mats** — power, per-zone temperature (0.5 °C) and heat level, single/double,
  seasonal heating/cooling, running/error state and over-temperature warning.
- **Flow** — automate both appliances: AirOne operating mode / fan speed / power /
  desired humidity, and sleep-mat power / season / per-zone temperature and heat level,
  as actions, with matching condition cards.
- **Mode-aware guidance** — a setting that doesn't apply to the current mode (e.g. humidity
  outside Dehumidify) is rejected with a toast that explains why, and the control reverts.
- **Shared account login** — all devices on the account share one session (Navien allows
  only one per account); sign in once from the app settings. Password-change repair is
  supported.

## Support

| Device | Status |
| --- | --- |
| AirOne — power, mode, fan, desired humidity | Supported (control verified on real hardware) |
| AirOne — air quality (PM, CO₂, TVOC, radon, temp/humidity, grade, filter, error) | Supported |
| AirMonitor — separate device, air-quality sensors | Supported |
| Sleep mat — power, per-zone temperature/level, heating/cooling, state | Supported (hardware-verified in the original project) |
| Boiler · wall pad | Out of scope |

> **Compatibility.** Only newer AirOne units (`modelCode ≥ 1000`) are supported. Older
> units use a completely different command envelope and topic scheme and cannot be reached
> this way.

## Setup

1. Install the app on your Homey.
2. Open the **app settings** and sign in with your **Navien Smart** account.
   (Note: Navien allows only one session per account, so opening the phone app may briefly
   log the app out — it re-logs-in automatically.)
3. Add a device → **Navien AirOne / AirMonitor / Sleep mat** → pick it from the account's
   device list. If no account is saved yet, sign in from the app settings first.
4. If you change your password, sign in again from the device's **repair** flow.

## Build

A Homey **Python runtime** app (SDK 3). Its only runtime dependencies are `paho-mqtt` and
`certifi`, declared in `app.json`'s `pythonPackages`.

```sh
homey app build     # compose .homeycompose/* into app.json and build python_packages
homey app install   # build and install to the connected Homey
homey app run        # dev mode with live logs (for diagnosing pairing/login)
python3 -m pytest -q # unit tests for the ported logic (navien_lib/navien/*)
```

Store and device images and icons are generated from the sources in `docs/` via
`scripts/make_images.py`.
