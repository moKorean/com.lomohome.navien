"""Unit tests for the ported Navien logic.

These exercise the pieces that do not need the Homey runtime: the AirOne model,
control-payload builders, MQTT message parsing, and the SigV4 presign. The
lib.airone.* modules import `homey` and are only checked on-device.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.navien import airone, mqtt
from lib.navien.api import AwsCredentials, NavienApi


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


def test_older_gen_is_skipped():
    raw = {"serviceCode": 300, "deviceId": "OLD",
           "Properties": {"modelCode": 500,
                          "data": {"did": {"reported": {"roomController": {"deviceId": "x"}}}}}}
    assert airone.AironeDevice.from_raw(raw) is None


def test_deep_merge_preserves_siblings():
    u = airone.AironeDevice.from_raw(_sample_raw())
    u.apply_reported({"roomController": {"option": 4}})
    assert u.mode == 9 and u.option == 4  # mode kept, only option changed


def test_control_payloads():
    u = airone.AironeDevice.from_raw(_sample_raw())
    power = u.desired_power(False)["roomController"]
    assert power["running"] == 2 and power["deviceId"] == "RC-77" and power["zoneId"] == 1

    mode = u.desired_mode(10)["roomController"]
    assert mode["mode"] == 10
    # Humidity is re-sent with the mode change so the device doesn't reset it.
    assert mode["additionalData"] == {"type": 1, "value": 55}


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
