"""Constants for the Navien Smart port.

Endpoints, setting/store keys, timings, and the AirOne (ventilation/dehumidify/
air-purify) value tables. Everything here comes from analysing the upstream
navien_smart_ha integration; see docs/PORTING.md for provenance.

Model-specific tables are deliberately *not* baked in beyond these enum labels:
the server tells us the selectable modes, fan steps, and humidity range per device,
and unknown values are skipped with a log line rather than guessed at.
"""

# --- Cloud endpoints -------------------------------------------------------
# Not the official API — the servers the "나비엔 스마트" iOS/Android app talks to.
API_URL = "https://nskr.naviensmartcontrol.com/api/v2.0"
LOGIN_URL = "https://member.naviensmartcontrol.com"
# The iOS app User-Agent. Required on every request; the server rejects unknown UAs.
USER_AGENT = "APP_NAVIENSMART_IOS"

# AWS IoT front used for the realtime state push. Note this is Navien's own domain,
# NOT the `network.server.endpoint` in a device's registry (that one is where the
# *appliance* connects; using it here gives an SNI mismatch / 403).
IOT_ENDPOINT = "nskr-iot.naviensmartcontrol.com"
IOT_PORT = 443
AWS_REGION = "ap-northeast-2"
AWS_SERVICE = "iotdevicegateway"

# Response codes the REST API returns in its envelope.
CODE_SUCCESS = 200
CODE_BAD_REQUEST = 400
CODE_NOT_AUTHORIZED = 404      # session invalidated (one session per account)
CODE_TOKEN_EXPIRED = 407

# --- Service codes ---------------------------------------------------------
SERVICE_MATE = 200             # sleep mat (out of scope for this app for now)
SERVICE_AIRONE = 300           # AirOne ventilation/dehumidify/air-purify — our target
TOPIC_PREFIX = {SERVICE_MATE: "mate", SERVICE_AIRONE: "airone"}

# AirOne REST control commands.
AIRONE_CMD_STATUS = "status"
AIRONE_CMD_POWER = "power"
AIRONE_CMD_CHANGE_MODE = "change-mode"
# cmd/rc/v2/{modelCode}/{physicalDeviceId}/remote/{command}
AIRONE_TOPIC_FMT = "cmd/rc/v2/{model_code}/{physical_device_id}/remote/{command}"

# Only newer-generation AirOne (V2.1) is supported; older units use a different
# envelope and inverted values.
AIRONE_V2_MIN_MODEL_CODE = 1000

# --- App-scoped settings (credentials live once, not per device) -----------
SETTING_USERNAME = "navien_username"
SETTING_PASSWORD = "navien_password"
SETTING_HOME_SEQ = "navien_home_seq"
SETTING_PAIR_ENV = "pair_env"
SETTING_UI_LANGUAGE = "ui_language"

# --- Per-device store keys -------------------------------------------------
STORE_DEVICE_SEQ = "device_seq"        # REST id, used in control URLs
STORE_DEVICE_ID = "device_id"          # id from the device list
STORE_PHYSICAL_ID = "physical_id"      # roomController.deviceId, used in topics
STORE_MODEL_CODE = "model_code"
STORE_SERVICE_CODE = "service_code"

# --- Timings ---------------------------------------------------------------
POLL_INTERVAL_S = 300.0                # REST re-read of device state / air sensors
INITIAL_STATE_TIMEOUT_S = 45.0         # warn if no reported state arrives after connect
AIRONE_READBACK_DELAY_S = 3.0          # re-request status this long after a command
MQTT_BACKOFF_S = (5, 15, 30, 60, 120, 300)

# --- AirOne running state (roomController.running; newer-gen convention) ----
RUNNING_ON = 1
RUNNING_OFF = 2
RUNNING_AWAY = 3
RUNNING_NAMES = {
    RUNNING_ON: {"en": "Running", "ko": "운전"},
    RUNNING_OFF: {"en": "Stopped", "ko": "정지"},
    RUNNING_AWAY: {"en": "Away", "ko": "외출"},
}

# --- AirOne operating modes (roomController.mode) --------------------------
MODE_NAMES = {
    4: {"en": "Ventilate", "ko": "환기"},
    5: {"en": "Exhaust", "ko": "배기"},
    6: {"en": "Cooking", "ko": "요리"},
    8: {"en": "Purify", "ko": "청정"},
    9: {"en": "Dehumidify", "ko": "제습"},
    10: {"en": "Ventilate + Dehumidify", "ko": "환기제습"},
    12: {"en": "Auto", "ko": "자동운전"},
    15: {"en": "Ventilate (outdoor)", "ko": "환기(외기)"},
    17: {"en": "Bypass", "ko": "바이패스"},
    18: {"en": "Negative-pressure ventilation", "ko": "음압환기"},
}
# Modes that carry a target-humidity setting.
MODES_WITH_HUMIDITY = {9, 10}

# --- AirOne option (roomController.option) ---------------------------------
OPTION_NONE = 1
OPTION_TURBO = 2
OPTION_SAVER = 3
OPTION_SLEEP = 4
OPTION_NAMES = {
    OPTION_NONE: {"en": "Normal", "ko": "옵션없음"},
    OPTION_TURBO: {"en": "Turbo", "ko": "터보"},
    OPTION_SAVER: {"en": "Saver", "ko": "절전"},
    OPTION_SLEEP: {"en": "Sleep", "ko": "숙면"},
}
# Fan speed is only separately selectable when option is Normal or Sleep.
OPTIONS_WITH_WIND = {OPTION_NONE, OPTION_SLEEP}

# --- AirOne fan / air volume (roomController.airVolume) --------------------
# Only values inside the server's `supportedAirVolumes` are ever used; this table
# is just for labels. Values not listed here are skipped (possible bit-mask).
AIR_VOLUME_NAMES = {
    1: {"en": "Low", "ko": "미풍"},
    2: {"en": "Medium", "ko": "약풍"},
    3: {"en": "High", "ko": "강풍"},
    4: {"en": "Auto", "ko": "자동"},
}

# --- Target humidity -------------------------------------------------------
HUMIDITY_TYPE = 1                      # additionalData.type meaning "target humidity"
HUMIDITY_STEP = 5                      # app -/+ button granularity
HUMIDITY_MIN_FALLBACK = 40             # used only if the server gives no range
HUMIDITY_MAX_FALLBACK = 70

# --- Air-quality sensors (REST /air-sensor) --------------------------------
# kind -> (title{en,ko}, unit, homey capability). Values with a Homey standard
# capability use it; the rest use a navien_* custom capability.
AIRONE_LEVEL_NAMES = {
    0: {"en": "Unknown", "ko": "알 수 없음"},
    1: {"en": "Good", "ko": "좋음"},
    2: {"en": "Moderate", "ko": "보통"},
    3: {"en": "Bad", "ko": "나쁨"},
    4: {"en": "Very bad", "ko": "매우 나쁨"},
}

# Normalisation of the server's sensor keys to our internal kind. Deliberately not a
# mechanical lower()/strip() — "pm1.0" vs "pm10" would collide.
AIRONE_SENSOR_ALIASES = {
    "pm1dot0": "pm1",
    "pm1.0": "pm1",
    "pm2dot5": "pm25",
    "pm2.5": "pm25",
    "pm10": "pm10",
    "co2": "co2",
    "tvoc": "tvoc",
    "radon": "radon",
    "temperature": "temperature",
    "humidity": "humidity",
    "total": "total",
}
