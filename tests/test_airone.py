"""Unit tests for the ported Navien logic.

These exercise the pieces that do not need the Homey runtime: the AirOne model,
control-payload builders, MQTT message parsing, and the SigV4 presign. The
lib.airone.* modules import `homey` and are only checked on-device.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from navien_lib.navien import airone, mqtt
from navien_lib.navien.api import AwsCredentials, NavienApi


def _sample_raw():
    return {
        "serviceCode": 300, "deviceSeq": 12345, "deviceId": "AIR-XYZ",
        "Properties": {
            "nickName": "거실 에어원", "modelCode": 1024,
            "data": {"did": {"reported": {
                "roomController": {"deviceId": "RC-77", "zoneId": 1, "running": 1,
                                   "mode": 9, "option": 1, "airVolume": 2,
                                   "additionalData": {"type": 1, "value": 55}},
                "odu": {"filter": [{"usage": {"percent": 42}}]},
            }}},
        },
    }


def test_from_raw_parses_newer_gen():
    u = airone.AironeDevice.from_raw(_sample_raw())
    assert u is not None
    assert u.physical_device_id == "RC-77"
    assert u.is_on and u.mode == 9 and u.air_volume == 2
    assert u.target_humidity == 55
    assert u.filters == [42]


def test_dict_nickname_uses_main_item():
    raw = {
        "serviceCode": 300, "deviceSeq": 1, "deviceId": "A",
        "Properties": {"nickName": {"mainItem": "제습환기"}, "modelCode": 1300,
                       "data": {"did": {"reported": {"roomController": {"deviceId": "RC"}}}}},
    }
    u = airone.AironeDevice.from_raw(raw)
    assert u is not None and u.nickname == "제습환기"


def test_older_gen_is_skipped():
    raw = {"serviceCode": 300, "deviceId": "OLD",
           "Properties": {"modelCode": 500,
                          "data": {"did": {"reported": {"roomController": {"deviceId": "x"}}}}}}
    assert airone.AironeDevice.from_raw(raw) is None


def test_deep_merge_preserves_siblings():
    u = airone.AironeDevice.from_raw(_sample_raw())
    u.apply_reported({"roomController": {"option": 4}})
    assert u.mode == 9 and u.option == 4  # mode kept, only option changed


def test_running_state_auto_dry():
    u = airone.AironeDevice.from_raw(_sample_raw())
    u.apply_reported({"roomController": {"running": 4}})
    assert u.running_name("ko") == "자동건조"
    assert u.status_text("ko") == "자동건조"   # only this, no mode/fan


def test_auto_dry_percent_reads_last_type4():
    u = airone.AironeDevice.from_raw(_sample_raw())
    # not auto-drying -> None even if a type-4 value is present
    u.apply_reported({"roomController": {"running": 1,
                                         "additionalData": [{"type": 4, "value": 30}]}})
    assert u.auto_dry_percent is None
    # auto-drying -> the last type-4 value wins
    u.apply_reported({"roomController": {"running": 4, "additionalData": [
        {"type": 3, "value": 55}, {"type": 4, "value": 30}, {"type": 4, "value": 47},
    ]}})
    assert u.auto_dry_percent == 47


def test_apply_reported_strips_capability_descriptors():
    """A status reply that echoes the whole DID (mode as a capability list,
    additionalData as a range table) must not clobber the live mode/humidity."""
    u = airone.AironeDevice.from_raw(_sample_raw())
    assert u.mode == 9 and u.target_humidity == 55
    u.apply_reported({"roomController": {
        "mode": [{"name": 9}, {"name": 10}],          # capability array, not the live int
        "additionalData": [{"type": 1, "min": 0, "max": 4}],  # range table, no value
        "running": 1,                                 # a real state field, kept
    }})
    assert u.mode == 9              # not overwritten by the list
    assert u.target_humidity == 55  # not overwritten by the range table
    assert u.running == 1


def test_control_payloads():
    u = airone.AironeDevice.from_raw(_sample_raw())
    power = u.desired_power(False)["roomController"]
    assert power["running"] == 2 and power["deviceId"] == "RC-77" and power["zoneId"] == 1

    # 제습(9) carries humidity, so it is re-sent with the mode change (device would
    # otherwise reset it); a non-humidity mode carries no humidity payload.
    mode = u.desired_mode(9)["roomController"]
    assert mode["mode"] == 9
    assert mode["additionalData"] == {"type": 1, "value": 55}
    assert "additionalData" not in u.desired_mode(10)["roomController"]


def test_mqtt_extract_and_parse():
    parsed = mqtt.extract_airone_reported(
        "100/airone/RC-77", {"reported": {"roomController": {"deviceId": "RC-77", "running": 1}}}
    )
    assert parsed[0] == "RC-77"
    assert airone.parse_air_sensors(
        [{"airs": [{"type": "pm2Dot5", "value": 12, "level": 1}]}]
    )["pm25"]["value"] == 12
    # A bare ack with no known section is ignored.
    assert mqtt.extract_airone_reported("t", {"reported": {"foo": 1}}) is None
    # The real envelope nests reported under "payload".
    nested = mqtt.extract_airone_reported(
        "361954/airone/68FE",
        {"topic": "dt/rc/v2/1901/68FE/status",
         "payload": {"reported": {"roomController": {"running": 1, "mode": 9}}},
         "serviceCode": 300})
    assert nested[0] == "68FE" and nested[1]["roomController"]["mode"] == 9


def test_sigv4_path_shape():
    creds = AwsCredentials("AKIA_TEST", "secret_test", "token/with+slash=")
    path = mqtt.build_signed_ws_path(creds, host="nskr-iot.naviensmartcontrol.com")
    assert path.startswith("/mqtt?X-Amz-Algorithm=AWS4-HMAC-SHA256")
    assert "X-Amz-Signature=" in path and "X-Amz-Security-Token=" in path


def test_desired_option_keeps_mode_and_humidity():
    u = airone.AironeDevice.from_raw(_sample_raw())
    d = u.desired_option(2)["roomController"]   # turbo
    assert d["mode"] == 9 and d["option"] == 2 and d["airVolume"] == 2
    assert d["additionalData"] == {"type": 1, "value": 55}   # humidity carried


def test_target_humidity_reads_type3_not_type1():
    """The device reports the value as type 3; a co-resident type-1 item (range 0-4)
    must be ignored via the bounds check, not read as the humidity."""
    raw = _sample_raw()
    room = raw["Properties"]["data"]["did"]["reported"]["roomController"]
    room["mode"] = 9
    room["additionalData"] = [
        {"type": 1, "value": 1},    # the 0-4 item — out of the 40-70 band
        {"type": 3, "value": 60},   # the real target humidity
    ]
    u = airone.AironeDevice.from_raw(raw)
    assert u.target_humidity == 60


def test_target_humidity_none_outside_humidity_modes():
    raw = _sample_raw()
    raw["Properties"]["data"]["did"]["reported"]["roomController"]["mode"] = 12  # 자동
    u = airone.AironeDevice.from_raw(raw)
    assert u.target_humidity is None


def test_humidity_range_from_server_metadata():
    raw = {
        "serviceCode": 300, "deviceSeq": 5, "deviceId": "A",
        "Properties": {"modelCode": 1200, "data": {"did": {"reported": {
            "roomController": {"deviceId": "RC", "mode": [
                {"name": 9, "additionalData": [{"type": 1, "min": 40, "max": 70}]},
                {"name": 10, "additionalData": [{"type": 1, "min": 30, "max": 60}]},
            ]}}}}},
    }
    u = airone.AironeDevice.from_raw(raw)
    assert u is not None
    assert u.humidity_range() == (30, 70)   # widened across modes
    # a unit with no metadata falls back to the default band
    assert airone.AironeDevice.from_raw(_sample_raw()).humidity_range() == (40, 70)


def test_airone_mode_metadata():
    modes = [
        {"name": 8, "option": 1, "supportedAirVolumes": [1, 2, 3, 4]},
        {"name": 9, "option": 1, "supportedAirVolumes": [4],
         "additionalData": [{"type": 1, "min": 40, "max": 70}]},
        {"name": 9, "option": 4},          # sleep on dehumidify
        {"name": 12, "option": 2},         # auto turbo
    ]
    raw = {
        "serviceCode": 300, "deviceSeq": 9, "deviceId": "A",
        "Properties": {"modelCode": 1300, "data": {"did": {"reported": {
            "roomController": {"deviceId": "RC", "mode": modes}}}}},
    }
    u = airone.AironeDevice.from_raw(raw)
    assert u is not None
    assert u.available_modes() == [8, 9, 12]
    assert u.available_options() == [1, 4, 2]
    assert u.available_air_volumes() == [1, 2, 3, 4]
    assert u.humidity_range() == (40, 70)


def test_air_monitors_and_per_monitor_parse():
    raw = {
        "serviceCode": 300, "deviceSeq": 7, "deviceId": "A",
        "Properties": {"modelCode": 1300, "data": {"did": {"reported": {
            "roomController": {"deviceId": "RC"},
            "airMonitor": [{"deviceId": "AM-1", "zoneId": 2, "modelCode": 35},
                           {"deviceId": "AM-2", "zoneId": 3}]}}}},
    }
    u = airone.AironeDevice.from_raw(raw)
    mons = u.air_monitors()
    assert len(mons) == 2 and mons[0]["monitor_id"] == "AM-1" and mons[0]["zone_id"] == 2
    sensor_list = [
        {"zoneId": 2, "airMonitor": {"deviceId": "AM-1"},
         "airs": [{"type": "pm2Dot5", "value": 9, "level": 1}]},
        {"zoneId": 3, "airMonitor": {"deviceId": "AM-2"},
         "airs": [{"type": "co2", "value": 800, "level": 2}]},
    ]
    parsed = airone.parse_air_sensors_for(sensor_list, zone_id=3, monitor_id="AM-2")
    assert parsed.get("co2", {}).get("value") == 800 and "pm25" not in parsed


def test_topic_slash_escaping():
    body = ('{"requestTopic":"cmd/rc/v2/1/RC/remote/power",'
            '"responseTopic":"cmd/rc/v2/1/RC/remote/power/res"}')
    esc = NavienApi._escape_topics(body, "cmd/rc/v2/1/RC/remote/power/res",
                                   "cmd/rc/v2/1/RC/remote/power")
    assert "cmd\\/rc\\/v2\\/1\\/RC\\/remote\\/power\\/res" in esc
    assert '"cmd\\/rc\\/v2\\/1\\/RC\\/remote\\/power"' in esc
