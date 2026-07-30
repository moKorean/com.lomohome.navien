"""AirOne (ventilation / dehumidify / air-purify) device model.

Ported from navien_smart_ha's `airone.py`. Holds the deep-merged `reported` state
and derives the handful of values this app exposes, plus builds the `desired`
payloads for the three control commands (power / change-mode).

The upstream author verified state parsing from real reports but could not verify
control, and only newer-generation units (modelCode >= 1000) are in scope — older
ones use inverted values and a different envelope. Unknown enum values are skipped,
not guessed. See docs/PORTING.md.
"""

from dataclasses import dataclass, field

from lib.const import (
    AIRONE_SENSOR_ALIASES,
    AIRONE_V2_MIN_MODEL_CODE,
    HUMIDITY_TYPE,
    MODES_WITH_HUMIDITY,
    OPTION_NONE,
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
    def from_raw(cls, raw: dict, log=print) -> "AironeDevice | None":
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
        model_code = _first(room, "modelCode") or _first(props, "modelCode") or _first(raw, "modelCode") or 0
        nickname = _first(props, "nickName", "nickname") or _first(raw, "nickName") or "Navien AirOne"

        if service_code is None and not room:
            return None  # not an AirOne at all
        try:
            model_code = int(model_code)
        except (TypeError, ValueError):
            model_code = 0
        if model_code and model_code < AIRONE_V2_MIN_MODEL_CODE:
            log(f"navien: skipping older-gen AirOne (modelCode {model_code} < {AIRONE_V2_MIN_MODEL_CODE})")
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
        self.air_sensors = parse_air_sensors(sensor_list)

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
    def mode(self):
        v = _first(self._room, "mode")
        return None if v is None else int(v)

    @property
    def option(self):
        v = _first(self._room, "option")
        return None if v is None else int(v)

    @property
    def air_volume(self):
        v = _first(self._room, "airVolume")
        return None if v is None else int(v)

    @property
    def target_humidity(self):
        """Target humidity if the current mode carries one, else None."""
        if self.mode not in MODES_WITH_HUMIDITY:
            return None
        extra = self._room.get("additionalData") or {}
        if isinstance(extra, list):
            extra = next((e for e in extra if e.get("type") == HUMIDITY_TYPE), {})
        if extra.get("type") == HUMIDITY_TYPE:
            try:
                return int(extra.get("value"))
            except (TypeError, ValueError):
                return None
        return None

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

    def desired_mode(self, mode: int, option: "int | None" = None) -> dict:
        """Change mode, carrying the current option/volume/humidity along.

        The device resets humidity to its minimum if a mode change omits it, so the
        current settings are always re-sent with the new mode.
        """
        room = self._room_base()
        room["mode"] = int(mode)
        room["option"] = int(option if option is not None else (self.option or OPTION_NONE))
        if self.air_volume is not None:
            room["airVolume"] = self.air_volume
        hum = self.target_humidity
        if mode in MODES_WITH_HUMIDITY and hum is not None:
            room["additionalData"] = {"type": HUMIDITY_TYPE, "value": hum}
        return {"roomController": room}

    def desired_fan(self, air_volume: int) -> dict:
        room = self._room_base()
        if self.mode is not None:
            room["mode"] = self.mode
        room["option"] = self.option or OPTION_NONE
        room["airVolume"] = int(air_volume)
        return {"roomController": room}

    def desired_humidity(self, value: int) -> dict:
        room = self._room_base()
        if self.mode is not None:
            room["mode"] = self.mode
        room["option"] = self.option or OPTION_NONE
        if self.air_volume is not None:
            room["airVolume"] = self.air_volume
        room["additionalData"] = {"type": HUMIDITY_TYPE, "value": int(value)}
        return {"roomController": room}


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
