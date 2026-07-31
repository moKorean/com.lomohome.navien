# Navien Smart Community (Homey)

Control your **Navien AirOne** ventilation / dehumidifier / air-purifier
(경동나비엔 나비엔 에어원 환기·제습·청정) from Homey. This app talks to the same
Navien cloud the official *나비엔 스마트* app uses — REST for control, AWS IoT MQTT for
realtime state. It is **not** an official Navien product.

## Credits & license

This is a Homey port of the **navien_smart_ha** Home Assistant integration.

- **Original author:** Eui Young Jung
- **Original project:** https://github.com/ripe-avocado/navien_smart_ha
- **License of the upstream work:** MIT

The upstream project is used under the MIT License. Its original copyright and
permission notice are reproduced in full in [`NOTICE`](./NOTICE), as the license
requires. All of the protocol knowledge in this app — the login flow, the MQTT
transport, and the AirOne value tables — derives from that project; see
[`docs/PORTING.md`](./docs/PORTING.md).

The Homey port itself is © 2026 Geunwon Mo.

## What works

| Feature | Status |
| --- | --- |
| AirOne power on/off | ported |
| Operating mode (ventilate / purify / dehumidify / auto …) | ported |
| Fan speed (low / medium / high / auto) | ported |
| Target humidity (dehumidify modes) | ported |
| Air quality — PM1/PM2.5/PM10, CO₂, temperature, humidity, filter usage | ported |
| Sleep mats (숙면매트) — power, per-zone temperature (0.5 °C) / level (1.0L), single & double, four-season heat/cool, operation & error status, high-temp alarm | ported (hardware-verified upstream) |
| Boilers, wall pads | out of scope for now |

> **Verification status.** Like the upstream project, the AirOne control path is
> reverse-engineered from the app and **not yet confirmed against a real appliance**.
> State reads are expected to work; control may need adjustment once tested on
> hardware. Only newer-generation AirOne units (`modelCode >= 1000`) are supported.

## Setup

1. Install the app on your Homey.
2. Open **app settings** and sign in with your **나비엔 스마트** account ID and password.
   (Note: Navien allows one active session per account, so opening the phone app may
   briefly log the Homey app out; it re-authenticates automatically.)
3. Add a device → **Navien AirOne** → it lists the AirOne units on your account.

## Building

This is a Homey **Python-runtime** app (SDK 3). The only runtime dependency is
`paho-mqtt`, declared in `app.json`'s `pythonPackages`.

```sh
homey app build     # merges .homeycompose/* into app.json, builds python_packages/*
homey app run       # run on a connected Homey
```

### Before publishing — TODO

- **Store & driver images are placeholders.** Generated PNGs are present at the
  required sizes (app `assets/images/{small,large,xlarge}.png` = 250×175 / 500×350 /
  1000×700; driver `drivers/airone/assets/images/{small,large,xlarge}.png` = 75×75 /
  500×500 / 1000×1000), but they are a simple generated logo — replace with real
  product imagery before publishing to the app store.
- Confirm control against a real AirOne unit and adjust `lib/navien/airone.py` if needed.
- Multi-home accounts currently default to the first home; a picker is TODO.
