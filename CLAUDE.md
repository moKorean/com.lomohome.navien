# CLAUDE.md — com.lomohome.navien

Homey (SDK3, Python runtime) app that controls **Navien AirOne** (ventilation /
dehumidify / air-purify). It is a port of the MIT-licensed Home Assistant integration
**navien_smart_ha** by Eui Young Jung (https://github.com/ripe-avocado/navien_smart_ha);
attribution is in `NOTICE` and `README.md`, and the protocol design is in `docs/PORTING.md`.

## Working with the maintainer's input

When working externally the maintainer often has **no Hangul IME**, so they type Korean
in **dubeolsik (2-set) layout with the keyboard in English mode**. Messages then look
like random Latin letters (e.g. `gksrmf` = 한글, `aksemfwk` = 만들자, `gownj` = 해줘) but
are real Korean.

- **Decode such input as Korean and reply in Korean.**
- A converter lives one level up, in the parent folder:
  `../qwerty_to_hangul.py` — `python3 ../qwerty_to_hangul.py "gksrmf aksemfwk"` → `한글 만들자`
  (or pipe text in on stdin). Note it transliterates *every* mapped letter, so literal
  English words/filenames mixed into a sentence get transliterated too.

## Layout

- `lib/navien/` — cloud client, no `homey` import (unit-testable):
  `api.py` (REST: 2-step login, device list, control, air-sensor), `mqtt.py`
  (AWS IoT MQTT-over-WS, SigV4 presigned), `airone.py` (state model + control payloads),
  `tls.py` (certifi-backed TLS context — the runtime has no system CA store).
- `lib/airone/` — Homey driver + device (imports `homey`; on-device only).
- `lib/` — `compat.py`, `i18n.py`, `const.py`, `selfcheck.py` (reused from localthings).
- `.homeycompose/` — manifest head + custom capabilities; **never hand-edit root `app.json`**
  (it is generated). `drivers/airone/` — driver shim, pair views, assets.

## Build / install

```sh
homey app build       # composes .homeycompose/* into app.json, builds python_packages
homey app install     # build + install to the connected Homey Pro
homey app run         # dev mode with live logs (use to diagnose pairing/login)
python3 -m pytest -q  # unit tests for the ported logic (lib/navien/*)
```

## Status

AirOne control is reverse-engineered and **not yet verified on real hardware** (state
reads expected to work; control may need tuning in `lib/navien/airone.py`). Only
newer-gen units (modelCode ≥ 1000). Sleep mats (serviceCode 200) are out of scope so far.
Public repo: https://github.com/moKorean/com.lomohome.navien
