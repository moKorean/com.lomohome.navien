"""Sleep-mat (숙면매트, serviceCode 200) device model.

Ported from navien_smart_ha's models.py / climate.py / select.py / switch.py.
Holds the deep-merged shadow `reported` state and derives per-zone values, plus builds
the `desired` payloads for the mat's controls. This is the upstream project's
hardware-verified product.

Key rules carried over (see docs/PORTING.md):
  * Zones: single ("single") or double ("left"/"right"), decided by mcu.capacity==2 or a
    nickName.side dict — never by the model name.
  * Type per HeatControl.unit: "0.5C" temperature mats vs "1.0L" level mats. Reads take
    whichever axis is present.
  * Four-season mats have a coolControl; range and behaviour switch with `season`
    (0 heat / 2 cool). Cooling mirrors an empty zone from the other side.
  * State is deep-merged (partial reports); control never optimistically updates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from navien_lib.const import (
    CAPACITY_DOUBLE,
    MAT_MODE_NAMES,
    MAT_MODES_ON,
    MAT_SEASON_NAMES,
    SEASON_SUMMER,
    SERVICE_MATE,
    UNIT_CELSIUS,
    UNIT_LEVEL,
    ZONE_LEFT,
    ZONE_RIGHT,
    ZONE_SINGLE,
)
from navien_lib.navien.airone import deep_merge


def _dig(d, *path, default=None):
    node = d
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return default if node is None else node


@dataclass
class HeatControl:
    unit: str | None
    range_min: float | None
    range_max: float | None
    safe_value: float | None
    enable_safe: bool
    fan_rpm: object = None
    anti_condensation: object = None

    @classmethod
    def parse(cls, fn) -> HeatControl | None:
        if not isinstance(fn, dict):
            return None
        return cls(
            unit=fn.get("unit"),
            range_min=fn.get("rangeMin"),
            range_max=fn.get("rangeMax"),
            safe_value=fn.get("safeValue"),
            enable_safe=bool(fn.get("enableSafe")),
            fan_rpm=fn.get("fanRPM"),
            anti_condensation=fn.get("antiCondensation"),
        )

    @property
    def is_level(self) -> bool:
        return self.unit == UNIT_LEVEL

    @property
    def is_celsius(self) -> bool:
        return self.unit == UNIT_CELSIUS

    @property
    def is_known(self) -> bool:
        return self.is_level or self.is_celsius

    @property
    def step(self) -> float:
        return 0.5 if self.is_celsius else 1.0


@dataclass
class MateDevice:
    device_seq: object
    device_id: str
    model_code: int
    model_name: str
    model_type: str
    nickname: str
    zone_names: dict           # zone key -> ko label
    heat_control: HeatControl | None
    cool_control: HeatControl | None
    has_power_ctrl: bool
    reported: dict = field(default_factory=dict)

    # --- construction ------------------------------------------------------

    @classmethod
    def from_raw(cls, raw: dict, log=print) -> MateDevice | None:
        service_code = raw.get("serviceCode")
        if service_code is None or int(service_code) != SERVICE_MATE:
            return None
        props = raw.get("Properties") or raw.get("properties") or {}
        attrs = _dig(props, "registry", "attributes", default={}) or {}
        functions = attrs.get("functions") or {}
        mcu = attrs.get("mcu") or {}

        device_id = str(raw.get("deviceId") or "")
        device_seq = raw.get("deviceSeq")
        if not device_id or device_seq is None:
            log("navien: mat entry missing deviceId/deviceSeq; skipping")
            return None

        model_code = raw.get("modelCode") or attrs.get("modelCode") or 0
        try:
            model_code = int(model_code)
        except (TypeError, ValueError):
            model_code = 0
        model_name = raw.get("modelName") or attrs.get("model") or "Navien Mat"
        model_type = attrs.get("modelType") or ""

        nick = props.get("nickName")
        side = nick.get("side") if isinstance(nick, dict) else None
        capacity = mcu.get("capacity")
        if capacity == CAPACITY_DOUBLE or isinstance(side, dict):
            side = side or {}
            zone_names = {
                ZONE_LEFT: side.get("left") or "좌측",
                ZONE_RIGHT: side.get("right") or "우측",
            }
        else:
            zone_names = {ZONE_SINGLE: "난방"}
        nickname = (nick.get("mainItem") if isinstance(nick, dict) else nick) or model_name

        return cls(
            device_seq=device_seq,
            device_id=device_id,
            model_code=model_code,
            model_name=str(model_name),
            model_type=str(model_type),
            nickname=str(nickname),
            zone_names=zone_names,
            heat_control=HeatControl.parse(functions.get("heatControl")),
            cool_control=HeatControl.parse(functions.get("coolControl")),
            has_power_ctrl=bool(functions.get("powerCtrl")),
        )

    def apply_reported(self, reported: dict) -> None:
        deep_merge(self.reported, reported or {})

    # --- top-level derived state ------------------------------------------

    @property
    def zones(self) -> tuple:
        return tuple(self.zone_names)

    @property
    def is_double(self) -> bool:
        return ZONE_LEFT in self.zone_names

    @property
    def is_four_season(self) -> bool:
        return self.cool_control is not None

    @property
    def operation_mode(self):
        v = self.reported.get("operationMode")
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @property
    def season(self):
        v = self.reported.get("season")
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @property
    def is_cooling(self) -> bool:
        return self.season == SEASON_SUMMER and self.cool_control is not None

    @property
    def active_control(self) -> HeatControl | None:
        return self.cool_control if self.is_cooling else self.heat_control

    @property
    def error_code(self):
        return self.reported.get("errorCode")

    @property
    def has_error(self) -> bool:
        try:
            return int(self.error_code) != 0
        except (TypeError, ValueError):
            return False

    @property
    def is_on(self):
        om = self.operation_mode
        return None if om is None else (om in MAT_MODES_ON)

    def mode_name(self, language: str = "en") -> str | None:
        om = self.operation_mode
        if om is None:
            return None
        if om == 1 and self.is_cooling:
            return {"en": "Cooling", "ko": "냉방"}.get(language, "Cooling")
        label = MAT_MODE_NAMES.get(om)
        if label:
            return label.get(language, label.get("en"))
        return f"알 수 없음 ({om})" if language == "ko" else f"Unknown ({om})"

    def season_id(self):
        s = self.season
        return None if s not in MAT_SEASON_NAMES else str(s)

    @property
    def over_safe_value(self) -> bool:
        if self.is_cooling:
            return False
        hc = self.heat_control
        if not hc or not hc.enable_safe or hc.safe_value is None:
            return False
        for zone in self.zones:
            setting = self.zone_setting(zone)
            if setting is not None and setting > hc.safe_value:
                return True
        return False

    # --- per-zone read (with cooling mirror) ------------------------------

    def _heater(self, zone) -> dict:
        return _dig(self.reported, "heater", zone, default={}) or {}

    def _setting_raw(self, zone):
        h = self._heater(zone)
        level = _dig(h, "level", "set")
        if level is not None:
            return level
        return _dig(h, "temperature", "set")

    def _current_raw(self, zone):
        return _dig(self._heater(zone), "temperature", "current")

    def _enabled_raw(self, zone):
        return self._heater(zone).get("enable")

    def _mirror(self, zone, getter):
        value = getter(zone)
        if value is None and self.is_cooling and self.is_double:
            other = ZONE_RIGHT if zone == ZONE_LEFT else ZONE_LEFT
            value = getter(other)
        return value

    def zone_setting(self, zone):
        return self._mirror(zone, self._setting_raw)

    def zone_current(self, zone):
        return self._mirror(zone, self._current_raw)

    def zone_enabled(self, zone):
        return self._mirror(zone, self._enabled_raw)

    # --- control payloads --------------------------------------------------

    def desired_power(self, on: bool) -> dict:
        return {"operationMode": 1 if on else 0}

    def build_heater_desired(self, changes: dict, enables=None, control=None) -> dict:
        """Rebuild the whole `heater` block, re-sending each zone's current value.

        The app sends every zone (not just the changed one) rather than trusting the
        shadow to merge, so this does the same.
        """
        control = control or self.active_control
        enables = enables or {}
        axis = "level" if (control and control.is_level) else "temperature"
        heater = {}
        for zone in self.zones:
            value = changes.get(zone)
            if value is None:
                value = self.zone_setting(zone)
            if value is None:
                continue
            number = int(value) if axis == "level" else float(value)
            if zone in enables:
                enabled = enables[zone]
            elif axis == "level" and zone in changes:
                enabled = number > 0
            else:
                current = self.zone_enabled(zone)
                enabled = True if current is None else bool(current)
            heater[zone] = {"enable": enabled, axis: {"set": number}}
        return {"heater": heater}

    def desired_temperature(self, zone, value) -> dict:
        d = self.build_heater_desired({zone: float(value)}, enables={zone: True})
        d["operationMode"] = 1
        return d

    def desired_level(self, zone, value) -> dict:
        value = int(value)
        return self.build_heater_desired({zone: value}, enables={zone: value > 0})

    def desired_zone_off(self, zone) -> dict:
        current = self.zone_setting(zone) or 0
        return self.build_heater_desired({zone: current}, enables={zone: False})

    def desired_season(self, season) -> dict:
        season = int(season)
        if season not in MAT_SEASON_NAMES:
            raise ValueError(f"unknown season {season}")
        return {"season": season}

    # --- Homey capability mapping -----------------------------------------

    def _zone_title(self, zone) -> dict:
        en = {ZONE_SINGLE: "Heater", ZONE_LEFT: "Left", ZONE_RIGHT: "Right"}.get(zone, zone)
        return {"en": en, "ko": self.zone_names.get(zone, en)}

    def _suffix(self, zone) -> str:
        return "" if zone == ZONE_SINGLE else f".{zone}"

    def homey_capabilities(self) -> list:
        caps = []
        if self.has_power_ctrl:
            caps.append("onoff")
        caps.append("navien_operation_mode")
        hc = self.heat_control
        for zone in self.zones:
            sfx = self._suffix(zone)
            if hc and hc.is_celsius:
                caps.append(f"target_temperature{sfx}")
                caps.append(f"measure_temperature{sfx}")
            elif hc and hc.is_level:
                caps.append(f"navien_heat_level{sfx}")
        if self.is_four_season:
            caps.append("navien_season")
        if hc and hc.enable_safe and hc.safe_value is not None:
            caps.append("alarm_heat")
        caps.append("alarm_generic")
        caps.append("navien_error_code")
        return caps

    def homey_capability_options(self) -> dict:
        opts = {}
        hc = self.heat_control
        active = self.active_control or hc
        for zone in self.zones:
            sfx = self._suffix(zone)
            title = self._zone_title(zone)
            if hc and hc.is_celsius:
                opts[f"target_temperature{sfx}"] = {
                    "title": title,
                    "min": (active.range_min if active else None) or 20,
                    "max": (active.range_max if active else None) or 45,
                    "step": 0.5,
                }
                opts[f"measure_temperature{sfx}"] = {"title": title}
            elif hc and hc.is_level:
                opts[f"navien_heat_level{sfx}"] = {
                    "title": title,
                    "min": 0,
                    "max": (hc.range_max or 9),
                    "step": 1,
                }
        return opts


def extract_mate_reported(topic: str, payload: dict):
    """`(device_id, reported)` from a mat shadow message, or None.

    Two gates, matching the app: the shadow topic must end with `/update/accepted`
    and `payload.state.reported` must be a dict — otherwise it's a command just landing
    in the shadow, and using it would get us ahead of the device.
    """
    shadow_topic = (payload or {}).get("topic") or ""
    if not shadow_topic.endswith("/update/accepted"):
        return None
    reported = _dig(payload, "payload", "state", "reported")
    if not isinstance(reported, dict):
        return None
    device_id = _dig(reported, "info", "deviceId") or topic.rsplit("/", 1)[-1]
    return device_id, reported
