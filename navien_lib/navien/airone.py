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
            reported=reported,
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
        deep_merge(self.reported, reported or {})

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
        """One-line '운전모드 · 풍량 · 옵션' text for a read-only status sensor."""
        parts = [self.mode_name(language), self.fan_name(language)]
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
        """`percent` used for each filter the outdoor unit reports."""
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
