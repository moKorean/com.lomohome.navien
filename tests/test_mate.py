"""Unit tests for the ported sleep-mat (Mate) logic."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from navien_lib.navien import mate
from navien_lib.navien.api import NavienApi


def _single_temp():
    return {
        "serviceCode": 200, "deviceSeq": 11, "deviceId": "MAT-1", "modelCode": 700,
        "Properties": {"nickName": "안방", "registry": {"attributes": {
            "modelType": "wm",
            "functions": {"powerCtrl": True, "heatControl": {
                "unit": "0.5C", "rangeMin": 20, "rangeMax": 45,
                "safeValue": 42, "enableSafe": True}},
            "mcu": {"capacity": 1}}}},
    }


def _double_level_four_season():
    functions = {
        "powerCtrl": True,
        "heatControl": {"unit": "1.0L", "rangeMin": 1, "rangeMax": 9},
        "coolControl": {"unit": "1.0L", "rangeMin": 1, "rangeMax": 6},
    }
    attributes = {"modelType": "fm", "functions": functions, "mcu": {"capacity": 2}}
    nick = {"mainItem": "거실", "side": {"left": "내쪽", "right": "신랑"}}
    return {
        "serviceCode": 200, "deviceSeq": 12, "deviceId": "MAT-2", "modelCode": 800,
        "Properties": {"nickName": nick, "registry": {"attributes": attributes}},
    }


def test_single_temperature_mat():
    m = mate.MateDevice.from_raw(_single_temp())
    assert m is not None and m.zones == ("single",) and not m.is_double
    assert m.heat_control.is_celsius and not m.is_four_season
    m.apply_reported({"operationMode": 1, "heater": {
        "single": {"enable": True, "temperature": {"set": 24.5, "current": 23.0}}}})
    assert m.is_on and m.zone_setting("single") == 24.5 and m.zone_current("single") == 23.0
    expect = {"onoff", "target_temperature", "measure_temperature", "alarm_heat"}
    assert expect <= set(m.homey_capabilities())
    assert m.desired_temperature("single", 30) == {
        "heater": {"single": {"enable": True, "temperature": {"set": 30.0}}}, "operationMode": 1}


def test_over_safe_value():
    m = mate.MateDevice.from_raw(_single_temp())
    m.apply_reported({"operationMode": 1, "heater": {"single": {"temperature": {"set": 43}}}})
    assert m.over_safe_value is True   # 43 > safeValue 42
    m.apply_reported({"heater": {"single": {"temperature": {"set": 40}}}})
    assert m.over_safe_value is False


def test_double_level_four_season_and_cooling_mirror():
    d = mate.MateDevice.from_raw(_double_level_four_season())
    assert d.is_double and d.zones == ("left", "right") and d.is_four_season
    caps = set(d.homey_capabilities())
    assert {"navien_heat_level.left", "navien_heat_level.right", "navien_season"} <= caps
    d.apply_reported({"season": 2, "operationMode": 1,
                      "heater": {"left": {"enable": True, "level": {"set": 3}}}})
    assert d.is_cooling and d.season_id() == "2"
    # cooling mirrors the populated zone onto the empty one
    assert d.zone_setting("right") == 3
    assert d.desired_level("left", 0)["heater"]["left"] == {"enable": False, "level": {"set": 0}}
    assert d.desired_season(0) == {"season": 0} and d.desired_season(2) == {"season": 2}


def test_partial_report_deep_merge():
    d = mate.MateDevice.from_raw(_double_level_four_season())
    d.apply_reported({"operationMode": 1, "season": 0,
                      "heater": {"left": {"level": {"set": 5}}, "right": {"level": {"set": 4}}}})
    d.apply_reported({"heater": {"right": {"level": {"set": 6}}}})   # partial
    assert d.operation_mode == 1 and d.season == 0        # not wiped
    assert d.zone_setting("left") == 5 and d.zone_setting("right") == 6


def test_mate_mqtt_parse():
    ok = mate.extract_mate_reported(
        "100/mate/MAT-1",
        {"topic": "$aws/things/MAT-1/shadow/name/status/update/accepted",
         "payload": {"state": {"reported": {"info": {"deviceId": "MAT-1"}, "operationMode": 1}}}})
    assert ok[0] == "MAT-1" and ok[1]["operationMode"] == 1
    # a /delta (not /accepted) is ignored
    assert mate.extract_mate_reported("t", {"topic": "x/update/delta", "payload": {}}) is None


# --- turning one zone off (navien_smart_ha issue #16) ------------------------


def _double_temp(power_ctrl=True):
    """Two temperature-controlled zones, `rangeMin` 28 — so `off_value` is 27.5."""
    functions = {
        "powerCtrl": power_ctrl,
        "heatControl": {"unit": "0.5C", "rangeMin": 28, "rangeMax": 50},
    }
    attributes = {"modelType": "wm", "functions": functions, "mcu": {"capacity": 2}}
    nick = {"mainItem": "안방", "side": {"left": "좌", "right": "우"}}
    return {
        "serviceCode": 200, "deviceSeq": 13, "deviceId": "MAT-3", "modelCode": 520,
        "Properties": {"nickName": nick, "registry": {"attributes": attributes}},
    }


def test_off_value_is_one_step_below_the_minimum():
    """Not 0. The appliance's "off" is `rangeMin - step` on both axes."""
    temp = mate.MateDevice.from_raw(_double_temp())
    assert temp.heat_control.off_value == 27.5          # 28 - 0.5
    level = mate.MateDevice.from_raw(_double_level_four_season())
    assert level.heat_control.off_value == 0            # 1 - 1.0, why level was right
    assert level.cool_control.off_value == 0


def test_power_switch_exists_even_when_the_server_says_no_power_ctrl():
    """`powerCtrl` must not gate `onoff` — it left EME-520 tiles with no power control.

    The field is still parsed, because diagnostics should keep showing what the server
    claims; it just no longer decides anything.
    """
    m = mate.MateDevice.from_raw(_double_temp(power_ctrl=False))
    assert m.has_power_ctrl is False
    assert "onoff" in m.homey_capabilities()


def test_temperature_zone_off_sends_the_off_value_not_the_current_one():
    """`enable: false` alone is ignored on the celsius axis; the value does the work."""
    m = mate.MateDevice.from_raw(_double_temp())
    m.apply_reported({"operationMode": 1, "heater": {
        "left": {"enable": True, "temperature": {"set": 33.0}},
        "right": {"enable": True, "temperature": {"set": 30.0}}}})

    desired = m.desired_zone_off("left")

    assert desired == {"heater": {
        "left": {"enable": False, "temperature": {"set": 27.5}},
        "right": {"enable": True, "temperature": {"set": 30.0}}}}
    # No `operationMode`: stopping a zone is not powering the appliance down.
    assert "operationMode" not in desired


def test_setting_a_temperature_at_or_below_the_floor_turns_the_zone_off():
    """The floor is how the picker expresses "off" — 27.5 must not be sent as a setpoint."""
    m = mate.MateDevice.from_raw(_double_temp())
    m.apply_reported({"operationMode": 1, "heater": {
        "left": {"enable": True, "temperature": {"set": 33.0}},
        "right": {"enable": True, "temperature": {"set": 30.0}}}})

    assert m.desired_temperature("left", 27.5) == m.desired_zone_off("left")
    # Anything above the floor is untouched, operating mode included.
    on = m.desired_temperature("left", 31.0)
    assert on["heater"]["left"] == {"enable": True, "temperature": {"set": 31.0}}
    assert on["operationMode"] == 1


def test_the_last_running_zone_is_refused_with_a_reason():
    """The appliance ignores it, so explain instead of sending a no-op."""
    m = mate.MateDevice.from_raw(_double_temp())
    m.apply_reported({"operationMode": 1, "heater": {
        "left": {"enable": False, "temperature": {"set": 27.5}},   # already off
        "right": {"enable": True, "temperature": {"set": 30.0}}}})

    assert m.zone_is_off("left") is True
    try:
        m.desired_zone_off("right")
    except mate.MateZoneOffRefused:
        pass
    else:
        raise AssertionError("stopping the only running zone should be refused")
    # The zone that is already off can still be re-sent — that is not the last one.
    assert m.desired_zone_off("left")["heater"]["left"]["temperature"]["set"] == 27.5


def test_a_zone_of_unknown_state_counts_as_running():
    """Unknown must not lock the user out: send it and let the appliance decide."""
    m = mate.MateDevice.from_raw(_double_temp())
    m.apply_reported({"operationMode": 1, "heater": {
        "right": {"enable": True, "temperature": {"set": 30.0}}}})

    assert m.zone_is_off("left") is None
    assert m.desired_zone_off("right")["heater"]["right"]["temperature"]["set"] == 27.5


def test_a_single_zone_mat_is_not_refused():
    """Deliberately unlike upstream, which refuses this too.

    A single-zone mat has no other zone, so the "something must stay on" rule would refuse
    every zone-off it could ever make — removing the only way that mat has had to stop
    heating. The measurement behind the rule comes from a dual-control mat, and the app's
    own guard reads as dual-only, so extending it here would be inference.
    """
    m = mate.MateDevice.from_raw(_single_temp())     # rangeMin 20 -> off_value 19.5
    m.apply_reported({"operationMode": 1, "heater": {
        "single": {"enable": True, "temperature": {"set": 24.5}}}})

    assert m.desired_zone_off("single") == {
        "heater": {"single": {"enable": False, "temperature": {"set": 19.5}}}}


def test_temperature_picker_can_reach_the_off_value():
    """A floor of `rangeMin` could neither turn a zone off nor display one that is."""
    m = mate.MateDevice.from_raw(_double_temp())
    opts = m.homey_capability_options()
    assert opts["target_temperature.left"]["min"] == 27.5
    assert opts["target_temperature.left"]["max"] == 50


def test_level_zone_off_is_unchanged():
    """A fence, not a feature: the level axis was already sending `rangeMin - step`."""
    d = mate.MateDevice.from_raw(_double_level_four_season())
    d.apply_reported({"operationMode": 1, "season": 0, "heater": {
        "left": {"enable": True, "level": {"set": 3}},
        "right": {"enable": True, "level": {"set": 4}}}})

    assert d.desired_level("left", 0)["heater"]["left"] == {
        "enable": False, "level": {"set": 0}}
    assert d.desired_level("left", 5)["heater"]["left"] == {
        "enable": True, "level": {"set": 5}}


def test_mate_control_raw_body():
    api = NavienApi(username="u", password="p")
    api.user_seq = "77"
    captured = {}

    async def fake(method, path, raw_body=None):
        captured["path"] = path
        captured["raw"] = raw_body
        return {}

    api._authed = fake
    asyncio.new_event_loop().run_until_complete(api.mate_control(
        device_seq=11, home_seq=100, device_id="MAT-1", model_code=700,
        service_code=200, desired={"operationMode": 1}))
    raw = captured["raw"]
    assert '"$aws\\/things\\/MAT-1\\/shadow\\/name\\/status\\/update"' in raw
    assert '"modelCode": 700' in raw and '"operationMode": 1' in raw
    assert "control?homeSeq=100&userSeq=77" in captured["path"]
