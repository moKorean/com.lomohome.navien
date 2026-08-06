"""AirOne (ventilation / dehumidify / air-purify) device model.

Ported from navien_smart_ha's `airone.py`. Holds the deep-merged `reported` state
and derives the handful of values this app exposes, plus builds the `desired`
payloads for the three control commands (power / change-mode).

The upstream author verified state parsing from real reports but could not verify
control, and only newer-generation units (modelCode >= 1000) are in scope — older
ones use inverted values and a different envelope. Unknown enum values are skipped,
not guessed. See docs/PORTING.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from navien_lib.const import (
    AIR_VOLUME_NAMES,
    AIRONE_AUTO_DRY_TYPE,
    AIRONE_SENSOR_ALIASES,
    AIRONE_V2_MIN_MODEL_CODE,
    HUMIDITY_MAX_FALLBACK,
    HUMIDITY_MIN_FALLBACK,
    HUMIDITY_REPORT_TYPE,
    HUMIDITY_TYPE,
    MODE_NAMES,
    MODES_WITH_HUMIDITY,
    OPTION_NAMES,
    OPTION_NONE,
    RUNNING_AUTO_DRY,
    RUNNING_NAMES,
    RUNNING_OFF,
    RUNNING_ON,
    SERVICE_AIRONE,
)


def deep_merge(base: dict, incoming: dict) -> dict:
    """Merge `incoming` into `base` recursively.

    Reports arrive partial — a command reply may carry only the fields that changed —
    so state must be merged, never replaced, or unrelated values would blink out.
    """
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def strip_capability_fields(incoming: dict) -> dict:
    """Drop the capability *descriptors* that ride along in some state replies.

    A `status` request is sometimes answered with the whole DID document, whose
    `roomController.mode` is the *supported-combinations array* rather than the current
    mode (an int), and whose `additionalData` is a range table
    (`{"type":1,"min":0,"max":4}`) rather than a value (`{"type":3,"value":40}`).
    Merging those clobbers the live mode/humidity — the mode blanks out and the fan
    picker goes unavailable. The capability list is already read from the device list, so
    here we discard it and keep only real state. Ported from navien_smart_ha (v0.13.2).
    """
    controller = incoming.get("roomController")
    if not isinstance(controller, dict):
        return incoming
    inner = dict(controller)
    changed = False
    if isinstance(inner.get("mode"), list):
        del inner["mode"]
        changed = True
    extra = inner.get("additionalData")
    if isinstance(extra, list) and not any(
        isinstance(item, dict) and "value" in item for item in extra
    ):
        del inner["additionalData"]
        changed = True
    if not changed:
        return incoming
    trimmed = dict(incoming)
    trimmed["roomController"] = inner
    return trimmed


def merge_additional_data(base, incoming) -> list:
    """Merge `roomController.additionalData` entries by `type` instead of replacing.

    `deep_merge` replaces a list wholesale, which is right for every other list in a
    report but wrong here: a report carries only the types it has news about, so a lone
    type-3 humidity echo would delete the type-4 auto-dry progress alongside it.

    This never removes an entry, and that is safe in both directions. A stale type-4 is
    invisible because `auto_dry_percent` returns None unless running == RUNNING_AUTO_DRY.
    A stale type-3 is always overwritten by the incoming one, via `target_humidity`'s
    range check and its HUMIDITY_REPORT_TYPE preference.
    """
    if isinstance(base, dict):
        base = [base]
    out = [entry for entry in (base or []) if isinstance(entry, dict)]
    for entry in incoming or []:
        if not isinstance(entry, dict):
            continue
        kind = _as_int(entry.get("type"))
        for i, existing in enumerate(out):
            if _as_int(existing.get("type")) == kind:
                out[i] = entry
                break
        else:
            out.append(entry)
    return out


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class AironeMode:
    """One entry of the server's `roomController.mode` capability metadata.

    This is the source of which modes / options / fan speeds a given unit actually
    supports. It is only present in the device-list response; the MQTT state later
    replaces `roomController.mode` with the current mode (an int), so it must be read
    before merging live state.
    """

    mode: int
    option: int
    air_volume: int | None
    supported_air_volumes: tuple
    humidity_min: int | None
    humidity_max: int | None

    @classmethod
    def parse(cls, raw) -> AironeMode | None:
        if not isinstance(raw, dict):
            return None
        mode = _as_int(raw.get("name"))
        if mode is None:
            return None
        option = _as_int(raw.get("option"))
        option = OPTION_NONE if option is None else option
        air_volume = _as_int(raw.get("airVolume"))
        if air_volume not in AIR_VOLUME_NAMES:
            air_volume = None
        supported = []
        for v in raw.get("supportedAirVolumes") or []:
            iv = _as_int(v)
            if iv in AIR_VOLUME_NAMES and iv not in supported:
                supported.append(iv)
        hmin = hmax = None
        extra = raw.get("additionalData") or []
        if isinstance(extra, dict):
            extra = [extra]
        for ad in extra:
            if isinstance(ad, dict) and ad.get("type") == HUMIDITY_TYPE:
                hmin, hmax = ad.get("min"), ad.get("max")
        return cls(mode, option, air_volume, tuple(supported), hmin, hmax)


@dataclass
class AironeDevice:
    device_seq: object
    device_id: str
    model_code: int
    nickname: str
    physical_device_id: str
    zone_id: object = None
    reported: dict = field(default_factory=dict)
    air_sensors: dict = field(default_factory=dict)   # kind -> {"value":.., "level":..}
    # The cloud's own "is this appliance online" flag, `raw["connected"]` — the one field
    # in `GET /devices` that is live rather than a capability descriptor, and the only
    # authoritative answer we have to "will a control POST actually arrive".
    #
    # Tri-state on purpose. Upstream stores `bool(raw.get("connected"))` (airone.py:461),
    # which fails *closed*: a firmware or account that simply omits the key would read as
    # permanently offline. Keeping None for "the key was not there" and testing
    # `is not False` at the use site fails *open* instead, so an absent field costs
    # nothing and only an explicit False takes the device away from the user.
    connected_registry: bool | None = None

    # --- construction ------------------------------------------------------

    @classmethod
    def from_raw(cls, raw: dict, log=print) -> AironeDevice | None:
        """Build from one entry of `GET /devices`, or None if not a supported AirOne.

        Extraction is defensive: the exact envelope of the device list is only known
        from analysis, so every field has fallbacks and a missing essential is logged.
        """
        props = raw.get("Properties") or raw.get("properties") or raw
        service_code = _first(raw, "serviceCode") or _first(props, "serviceCode")
        if service_code is not None and int(service_code) != SERVICE_AIRONE:
            return None

        reported = cls._reported_of(props)
        room = reported.get("roomController") or {}
        # `_first` skips a key whose value is None but keeps an explicit False, which is
        # exactly the distinction `connected_registry` is built on.
        connected = _first(raw, "connected")
        if connected is None:
            connected = _first(props, "connected")

        device_seq = _first(raw, "deviceSeq", "device_seq") or _first(props, "deviceSeq")
        device_id = str(_first(raw, "deviceId") or _first(props, "deviceId") or "")
        physical = str(_first(room, "deviceId") or device_id or "")
        # The control topic uses the top-level device modelCode (matches the app);
        # roomController.modelCode can differ and would address the wrong device.
        model_code = (
            _first(raw, "modelCode") or _first(props, "modelCode")
            or _first(room, "modelCode") or 0
        )
        nick = _first(props, "nickName", "nickname") or _first(raw, "nickName")
        # nickName can be a dict like {"mainItem": "제습환기"}; take the label, not the dict.
        if isinstance(nick, dict):
            nick = nick.get("mainItem") or next(
                (v for v in nick.values() if isinstance(v, str)), None)
        nickname = nick or "Navien AirOne"

        if service_code is None and not room:
            return None  # not an AirOne at all
        try:
            model_code = int(model_code)
        except (TypeError, ValueError):
            model_code = 0
        if model_code and model_code < AIRONE_V2_MIN_MODEL_CODE:
            log(f"navien: skipping older-gen AirOne (modelCode {model_code} < "
                f"{AIRONE_V2_MIN_MODEL_CODE})")
            return None
        if not device_seq or not physical:
            log(f"navien: AirOne entry missing deviceSeq/physicalId; skipping ({device_id!r})")
            return None

        return cls(
            device_seq=device_seq,
            device_id=device_id or physical,
            model_code=model_code,
            nickname=str(nickname),
            physical_device_id=physical,
            zone_id=_first(room, "zoneId"),
            # Not inert, despite carrying no live state: `_poll_once` reads
            # `humidity_range()` and `wants_air_sensors()` off the unit built here, and
            # both traverse `reported`. Dropping it would send every unit to the 40/70
            # humidity fallback (this transient unit is the app's only path to the server's
            # real range — the live model's `roomController.mode` is an int, so its own
            # humidity_range() always falls back) and would make `wants_air_sensors()`
            # unconditionally True, restoring the wasted /air-sensor call on ventilators
            # with no monitor. Merging it into live state is the separate mistake, fenced
            # in airone/device.py's `_poll_once`.
            reported=reported,
            # Coerced to bool only when the key is actually present; see the field comment
            # for why the absent case must stay None rather than collapsing to False.
            connected_registry=(None if connected is None else bool(connected)),
        )

    @staticmethod
    def _reported_of(props: dict) -> dict:
        data = props.get("data") or {}
        did = data.get("did") or {}
        for candidate in (did.get("reported"), data.get("reported"), props.get("reported")):
            if isinstance(candidate, dict):
                return dict(candidate)
        return {}

    def apply_reported(self, reported: dict) -> None:
        r"""Deep-merge a report into the live state.

        `roomController.additionalData` needs two extra steps, both forced by our own
        control path rather than by the server: `_change_mode` sends that field as a bare
        dict (:427 and :444 — hardware-verified, so the wire shape stays) and `_optimistic`
        feeds the very same dict straight back in here (airone/device.py:380). Coercing it
        to a one-item list and then merging by `type` stops that echo from replacing the
        whole list, which is what deleted the type-4 entry and quietly degraded
        '자동건조 (90%)' to '자동건조'.

        Order: the coercion runs *after* `strip_capability_fields`. Either order gives the
        same result — the strip only inspects `additionalData` when it is already a list,
        so a dict passes through it untouched — but it is stated here rather than left
        implicit, and running it second means the strip keeps seeing the shape the server
        actually sent.

        The incoming dict is copied, never mutated: `_optimistic` reads its `desired` back
        afterwards (airone/device.py:381 → `_pending_from_desired`), which expects the
        dict form and would lose the pending humidity if this rewrote it in place.
        """
        incoming = strip_capability_fields(reported or {})
        room = incoming.get("roomController")
        if isinstance(room, dict) and "additionalData" in room:
            extras = room["additionalData"]
            if isinstance(extras, dict):
                extras = [extras]
            room = dict(room)
            room["additionalData"] = merge_additional_data(
                self._room.get("additionalData"), extras)
            incoming = dict(incoming)
            incoming["roomController"] = room
        deep_merge(self.reported, incoming)

    def wants_air_sensors(self) -> bool:
        """Whether it's worth asking this unit for air quality.

        Only skip when we're sure there's nothing: no attached AirMonitor and the room
        controller declared an empty `sensor` list. A missing list means "unknown", so we
        still ask. A ventilator with no monitor is what this spares. From navien_smart_ha.
        """
        if self.air_monitors():
            return True
        sensor = self._room.get("sensor")
        if not isinstance(sensor, list):
            return True
        return bool(sensor)

    def apply_air_sensors(self, sensor_list: list) -> None:
        # Merge, don't replace: an occasional empty/partial poll must not wipe the
        # values that are already showing.
        parsed = parse_air_sensors(sensor_list)
        if parsed:
            self.air_sensors.update(parsed)

    # --- derived state -----------------------------------------------------

    @property
    def _room(self) -> dict:
        return self.reported.get("roomController") or {}

    @property
    def _odu(self) -> dict:
        return self.reported.get("odu") or {}

    @property
    def running(self):
        value = _first(self._room, "running")
        if value is None:
            value = _first(self._odu, "running")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @property
    def is_on(self) -> bool:
        return self.running == RUNNING_ON

    @property
    def auto_dry_percent(self):
        """Auto-dry progress (%) while running == 4, else None.

        The app shows it inside the status line ("자동건조 중 47%"); we keep the status
        text as just '자동건조' and expose the number separately. Read the last
        additionalData type-4 entry, like the app. Ported from navien_smart_ha.
        """
        if self.running != RUNNING_AUTO_DRY:
            return None
        extras = self._room.get("additionalData") or []
        # Normalise the dict form the way target_humidity (:363-365) and AironeMode.parse
        # (:134-135) already do. Without it `reversed()` yields the dict's *keys*, every
        # isinstance check below fails, and the percentage silently disappears.
        if isinstance(extras, dict):
            extras = [extras]
        for extra in reversed(extras):
            if isinstance(extra, dict) and _as_int(extra.get("type")) == AIRONE_AUTO_DRY_TYPE:
                return _as_int(extra.get("value"))
        return None

    def running_name(self, language: str = "en"):
        """Localized running-state name; unknown codes shown as 'State (n)'."""
        r = self.running
        if r is None:
            return None
        label = RUNNING_NAMES.get(r)
        if label:
            return label.get(language, label.get("en"))
        return f"상태 ({r})" if language == "ko" else f"State ({r})"

    @staticmethod
    def _named(value, table, language, ko_prefix, en_prefix):
        if value is None:
            return None
        label = table.get(value)
        if label:
            return label.get(language, label.get("en"))
        return f"{ko_prefix} ({value})" if language == "ko" else f"{en_prefix} ({value})"

    def mode_name(self, language: str = "en"):
        return self._named(self.mode, MODE_NAMES, language, "모드", "Mode")

    def fan_name(self, language: str = "en"):
        return self._named(self.air_volume, AIR_VOLUME_NAMES, language, "풍량", "Fan")

    def option_name(self, language: str = "en"):
        label = OPTION_NAMES.get(self.option)
        return label.get(language, label.get("en")) if label else None

    def status_text(self, language: str = "en"):
        """One-line status for a read-only sensor.

        While auto-drying the mode/fan aren't meaningful, so show '자동건조' with the
        progress in parentheses — '자동건조 (90%)'. Otherwise it's 'mode · fan · option',
        and in 제습 the target humidity is appended to the mode — '제습(40%) · 강풍'.
        (The running-state sensor stays a plain name for string-compare automations.)
        """
        r = self.running
        if r == RUNNING_AUTO_DRY:
            name = self.running_name(language)
            pct = self.auto_dry_percent
            return f"{name} ({pct}%)" if pct is not None else name
        if r is not None and r != RUNNING_ON:
            # not actively running (정지 / 외출) — the mode/fan aren't the story
            return self.running_name(language)
        mode = self.mode_name(language)
        hum = self.target_humidity
        if mode and hum is not None:
            mode = f"{mode}({hum}%)"
        parts = [mode, self.fan_name(language)]
        if self.option not in (None, OPTION_NONE):
            parts.append(self.option_name(language))
        parts = [p for p in parts if p]
        return " · ".join(parts) if parts else None

    @property
    def mode(self):
        # roomController.mode is an int in live state, but a metadata list in the
        # device-list response before MQTT overwrites it — tolerate both.
        return _as_int(_first(self._room, "mode"))

    @property
    def option(self):
        return _as_int(_first(self._room, "option"))

    @property
    def air_volume(self):
        return _as_int(_first(self._room, "airVolume"))

    @property
    def target_humidity(self):
        """Target humidity if the current mode carries one, else None.

        The value comes back as an `additionalData` entry inside the mode's humidity
        range — reported as type 3, though a type-1 (range 0-4) item shares the list, so
        the range check is what actually disambiguates. Prefer the confirmed type 3 when
        several entries qualify. Mirrors navien_smart_ha's target_humidity.
        """
        if self.mode not in MODES_WITH_HUMIDITY:
            return None
        low, high = self.humidity_range()
        extras = self._room.get("additionalData") or []
        if isinstance(extras, dict):
            extras = [extras]
        candidates = []
        for extra in extras:
            if not isinstance(extra, dict):
                continue
            value = _as_int(extra.get("value"))
            if value is None or not low <= value <= high:
                continue
            candidates.append((_as_int(extra.get("type")), value))
        if not candidates:
            return None
        for kind, value in candidates:
            if kind == HUMIDITY_REPORT_TYPE:
                return value
        return candidates[0][1]

    @property
    def error_code(self):
        return (self._room.get("error") or self._odu.get("error") or {}).get("code")

    @property
    def has_error(self) -> bool:
        try:
            return int(self.error_code) != 0
        except (TypeError, ValueError):
            return False

    @property
    def filters(self) -> list:
        """Filter life **remaining**, as a percent, one per filter the outdoor unit reports.

        **The wire field lies.** It is `odu.filter[i].usage.percent`, and both this port
        and navien_smart_ha read that name as "how much of the filter is used up". It is
        the opposite: measured against the Navien app on a real unit, a reading of 87 is
        87% of the filter's life *left* (13% used). The capability keeps its
        `navien_filter_usage` id — renaming it would drop the sensor from every paired
        device and take the Insights history with it — but every label says 잔량.
        """
        out = []
        for f in self._odu.get("filter") or []:
            usage = f.get("usage") or {}
            pct = usage.get("percent", f.get("percent"))
            if pct is not None:
                out.append(int(pct))
        return out

    # --- control payloads --------------------------------------------------

    def _room_base(self) -> dict:
        base = {"deviceId": self.physical_device_id}
        if self.zone_id is not None:
            base["zoneId"] = self.zone_id
        return base

    def desired_power(self, on: bool) -> dict:
        room = self._room_base()
        room["running"] = RUNNING_ON if on else RUNNING_OFF
        return {"roomController": room}

    def _change_mode(self, mode, option, air_volume, humidity_mode) -> dict:
        """A change-mode `roomController`. Unlike power, it carries NO deviceId/zoneId
        (matching the app) — only mode/option, plus airVolume and humidity when they
        apply. Re-sending the current humidity stops the device resetting it."""
        room = {"option": int(option)}
        if mode is not None:
            room["mode"] = int(mode)
        if air_volume is not None:
            room["airVolume"] = int(air_volume)
        hum = self.target_humidity
        if humidity_mode in MODES_WITH_HUMIDITY and hum is not None:
            room["additionalData"] = {"type": HUMIDITY_TYPE, "value": hum}
        return {"roomController": room}

    def desired_mode(self, mode: int, option: int = OPTION_NONE) -> dict:
        # Picking a mode resets the option to normal; sleep/turbo/saver are chosen
        # separately (sleep via the mode list, turbo/saver via the fan list).
        return self._change_mode(mode, option, self.air_volume, int(mode))

    def desired_fan(self, air_volume: int) -> dict:
        # Picking an air volume clears turbo/saver (option -> normal).
        return self._change_mode(self.mode, OPTION_NONE, air_volume, self.mode)

    def desired_option(self, option: int) -> dict:
        return self._change_mode(self.mode, option, self.air_volume, self.mode)

    def desired_humidity(self, value: int) -> dict:
        room = self._change_mode(self.mode, self.option or OPTION_NONE, self.air_volume, self.mode)
        room["roomController"]["additionalData"] = {"type": HUMIDITY_TYPE, "value": int(value)}
        return room

    # --- server-provided metadata -----------------------------------------

    @property
    def modes(self) -> tuple:
        """The parsed `roomController.mode` capability list (device-list only)."""
        raw_modes = self._room.get("mode")
        if not isinstance(raw_modes, list):
            return ()
        return tuple(m for m in (AironeMode.parse(md) for md in raw_modes) if m)

    def available_modes(self) -> list:
        """Distinct operating modes the server says this unit supports, in order."""
        out = []
        for m in self.modes:
            if m.mode not in out:
                out.append(m.mode)
        return out

    def available_options(self) -> list:
        out = []
        for m in self.modes:
            if m.option not in out:
                out.append(m.option)
        return out

    def available_air_volumes(self) -> list:
        out = []
        for m in self.modes:
            for v in m.supported_air_volumes:
                if v not in out:
                    out.append(v)
            if m.air_volume is not None and m.air_volume not in out:
                out.append(m.air_volume)
        return sorted(out)

    def air_monitors(self) -> list:
        """Standalone AirMonitor units attached to this device (separate hardware)."""
        out = []
        raw = self.reported.get("airMonitor")
        if not isinstance(raw, list):
            return out
        for i, m in enumerate(raw):
            if not isinstance(m, dict):
                continue
            out.append({
                "monitor_id": str(m.get("deviceId") or f"{self.device_id}_airmonitor_{i}"),
                "zone_id": m.get("zoneId"),
                "model_code": _as_int(m.get("modelCode")) or 0,
                "version": m.get("version"),
            })
        return out

    def humidity_range(self) -> tuple:
        """(min, max) target humidity from the server's mode metadata.

        The device only ever reports the *current* humidity; the allowed range comes
        from `roomController.mode[].additionalData` (type == humidity). Widen across all
        modes that carry one; fall back to a sane default if the server gave none.
        """
        low = high = None
        modes = self._room.get("mode")
        if isinstance(modes, list):
            for md in modes:
                extra = md.get("additionalData") or []
                if isinstance(extra, dict):
                    extra = [extra]
                for ad in extra:
                    if ad.get("type") != HUMIDITY_TYPE:
                        continue
                    lo, hi = ad.get("min"), ad.get("max")
                    if lo is None or hi is None:
                        continue
                    low = lo if low is None else min(low, lo)
                    high = hi if high is None else max(high, hi)
        if low is None or high is None:
            return (HUMIDITY_MIN_FALLBACK, HUMIDITY_MAX_FALLBACK)
        return (low, high)


def parse_air_sensors(sensor_list: list) -> dict:
    """Flatten `GET /air-sensor`'s `sensorList[].airs[]` into `{kind: {value, level}}`.

    Sensor keys are normalised through an alias table, not mechanically, so `pm1.0`
    and `pm10` don't collide.
    """
    out: dict = {}
    for sensor in sensor_list or []:
        for air in sensor.get("airs") or []:
            raw_type = str(air.get("type") or "").strip()
            kind = AIRONE_SENSOR_ALIASES.get(raw_type.lower())
            if not kind:
                continue
            out[kind] = {"value": air.get("value"), "level": air.get("level")}
    return out


def air_sensor_changes(previous: dict, current: dict) -> str:
    """`kind=value` for every reading whose value moved, in a stable order.

    A log line, which is why it returns text and not a diff: both devices read the same
    `/air-sensor` endpoint and the open question about them (does the AirMonitor report on
    its own, or only through the AirOne?) is answered by watching whether these values move
    independently. Changed readings only, so a healthy poll costs one short line and a
    frozen feed is visible as the absence of any.
    """
    parts = []
    for kind in sorted(current):
        new = (current.get(kind) or {}).get("value")
        old = (previous.get(kind) or {}).get("value")
        if new != old:
            parts.append(f"{kind}={new}")
    return " ".join(parts)


def parse_air_sensors_for(sensor_list: list, zone_id=None, monitor_id=None) -> dict:
    """Air readings for one AirMonitor: pick its `sensorList` entry, then flatten.

    Matches on zoneId first, then the entry's `airMonitor.deviceId`; falls back to the
    first entry so a single-monitor account still populates.
    """
    chosen = None
    zone = None if zone_id is None else str(zone_id)
    for sensor in sensor_list or []:
        # zoneId comes back as a string ("1"); compare as strings.
        if zone is not None and str(sensor.get("zoneId")) == zone:
            chosen = sensor
            break
        monitor = sensor.get("airMonitor") or {}
        if monitor_id and str(monitor.get("deviceId")) == str(monitor_id):
            chosen = sensor
            break
    if chosen is None and sensor_list:
        chosen = sensor_list[0]
    return parse_air_sensors([chosen] if chosen else [])
