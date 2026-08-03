# CLAUDE.md — com.lomohome.navien

Homey (SDK3, Python runtime) app that controls **Navien AirOne** (ventilation /
dehumidify / air-purify), the **AirMonitor** air-quality sensor, and Navien
**sleep mats (숙면매트)**. It is a port of the MIT-licensed Home Assistant integration
**navien_smart_ha** by Eui Young Jung (https://github.com/ripe-avocado/navien_smart_ha);
attribution is in `NOTICE` and `README.md`, and the protocol design is in `docs/PORTING.md`.

## Working with the maintainer's input

When working externally the maintainer sometimes has **no Hangul IME**, so they type Korean
in **dubeolsik (2-set) layout with the keyboard in English mode**. Messages then look
like random Latin letters (e.g. `gksrmf` = 한글, `aksemfwk` = 만들자, `gownj` = 해줘) but
are real Korean.

- **Decode such input as Korean and reply in Korean.**
- A converter lives one level up, in the parent folder:
  `../qwerty_to_hangul.py` — `python3 ../qwerty_to_hangul.py "gksrmf aksemfwk"` → `한글 만들자`
  (or pipe text in on stdin). Note it transliterates *every* mapped letter, so literal
  English words/filenames mixed into a sentence get transliterated too.

## Layout

The Python package is `navien_lib` (**not** `lib`).

- `navien_lib/navien/` — cloud client, no `homey` import (unit-testable):
  `api.py` (REST: 2-step login, device list, control, air-sensor with a 60s cache),
  `mqtt.py` (AWS IoT MQTT-over-WS, SigV4 presigned), `airone.py` (AirOne state model +
  control payloads, auto-dry progress, status text), `mate.py` (sleep-mat state/control),
  `tls.py` (certifi-backed TLS context — the runtime has no system CA store).
- `navien_lib/airone/`, `navien_lib/airmonitor/`, `navien_lib/mate/` — one Homey
  driver + device per device type (imports `homey`; on-device only). Each registers its
  own Flow cards.
- `navien_lib/` — `pairing.py` (shared install / repair handlers), `compat.py`
  (`shared_api`, `reauth_shared_api`, Flow helpers), `i18n.py`, `const.py`, `selfcheck.py`.
- `app.py` — `NavienApp` (homey_export). Owns ONE **app-level shared session**:
  `shared_api()` returns a single stable `NavienApi` object shared by every device
  (creds updated in place, never replaced); `reauth()` re-logs-in for repair.
- `.homeycompose/` — manifest head, custom `capabilities/*.json`, and
  `flow/{actions,conditions}/*.json`. **Never hand-edit root `app.json`** (it is generated).
  `drivers/<id>/` — driver shim, pair views, assets.

## Build / install / publish

```sh
homey app validate --level publish   # validate before committing docs/manifest changes
homey app build                      # composes .homeycompose/* into app.json
homey app install                    # build + install to the connected Homey Pro
homey app run                        # dev mode with live logs (diagnose pairing/login)
uv run pytest -q                     # unit tests for the ported logic (navien_lib/navien/*)
```

**Deploy ("배포")** is a fixed sequence — bump `version` in `.homeycompose/app.json` by
hand, add a KO+EN `.homeychangelog.json` entry, refresh `README.md`/`README.en.md`, commit
+ push, then `homey app publish` (answer guidelines prompt **y**, "update version?" **n**),
then `homey app install`. See the maintainer's memory for the exact expect script. Store
text (`README.txt`, `README.ko.txt`, app name/description, changelog) must contain **no
"Homey"** (App Store rejects it); GitHub `README.md`/`README.en.md` may mention it. Never
add a `Co-Authored-By: Claude ...` trailer to commits.

## Status

- **AirOne** — control **verified on real hardware** (Geunwon's Homey Pro). Composite
  mode/fan pickers, 희망습도 (dehumidify mode only), 자동건조 progress (`자동건조 (90%)`),
  정지/외출 running states, optimistic settle with confirm-release. Newer-gen units only
  (modelCode ≥ 1000).
- **AirMonitor** — air-quality sensor, exposed as its own device.
- **Sleep mats (숙면매트, serviceCode 200)** — supported with power/season/temperature/
  level control and Flow cards.

All device types share the single app-level session and support Flow cards. Public repo:
https://github.com/moKorean/com.lomohome.navien
