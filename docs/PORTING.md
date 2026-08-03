# Navien Smart — Homey 포팅 설계 노트

이 문서는 `com.lomohome.navien` (Homey 앱)이 어떤 상위 프로젝트를 포팅한 것인지,
그리고 나비엔 클라우드와 통신하는 규약을 정리한다. 원 프로젝트의 소스 코드를 분석해
Homey SDK3 Python 런타임으로 옮긴다.

## 출처 / 라이선스

- **원작자:** Eui Young Jung
- **원 프로젝트:** navien_smart_ha (Home Assistant custom integration)
- **소스:** https://github.com/ripe-avocado/navien_smart_ha
- **라이선스:** MIT — 원 저작권/허가 고지는 저장소 루트 `NOTICE` 에 전문 보존.

공식 API 가 아니라 경동나비엔 **나비엔 스마트** 앱이 사용하는 서버를 그대로 사용하는
리버스 엔지니어링 구현이다. 에어원(환기·제습·청정)·에어모니터·매트(serviceCode 200) 모두
실기기(Homey Pro)에서 상태 수신과 제어를 검증했다.

---

## 1. 인증 (REST, 2단계 로그인)

사용자 입력은 **아이디/비밀번호** 뿐. 토큰은 저장하지 않고 매 setup 마다 로그인으로 취득한다.

엔드포인트:
- `API_URL  = https://nskr.naviensmartcontrol.com/api/v2.0`
- `LOGIN_URL = https://member.naviensmartcontrol.com`
- `User-Agent`: iOS 앱 UA (`...APP_NAVIENSMART_IOS`) — 모든 요청에 필수.

1. **폼 로그인** `POST {LOGIN_URL}/member/login` (form `username`/`password`, 쿠키 세션,
   `Origin`/`Referer` 필요). 응답 HTML 의 `var message = {...}` 파싱 → `accessToken`,
   `refreshToken`, `loginId`, `userSeq`(= account_seq).
2. **secured-sign-in** `POST /users/secured-sign-in` (헤더 `Authorization: {accessToken}`,
   body `{"userId": loginId, "accountSeq": userSeq}`) → `home[]`, `userInfo.userSeq`(= **user_seq**,
   1단계와 다른 값), `authInfo`(AWS IoT 임시 자격증명: accessKeyId/secretKey/sessionToken).

주의:
- `account_seq`(1단계 userSeq) ≠ `user_seq`(2단계 userInfo.userSeq). MQTT clientId·REST 쿼리엔 user_seq.
- **AWS 자격증명 갱신은 secured-sign-in 재호출로만.** `/auth/token/refresh` 는 accessToken 만 줌.
- **계정당 세션 1개** — 앱을 열면 404(NOT_AUTHORIZED) 흔함 → 자동 재로그인 후 1회 재시도.
- 응답 코드: 200 성공 / 400 bad request / 404 not authorized / 407 token expired.

Config 에 저장: `username`, `password`, `home_seq`.

## 2. 기기 목록 / 제어 (REST)

- 목록: `GET {API_URL}/devices?homeSeq=&userSeq=` → `data.devices[]`.
- 제어: `POST {API_URL}/devices/{deviceSeq}/control?homeSeq=&userSeq=`, `Authorization: {accessToken}`.
  - **topic 문자열의 `/` 를 `\/` 로 이스케이프한 raw body** 를 그대로 보내야 서버가 받는다.
- 공기질: `GET {API_URL}/devices/{deviceSeq}/air-sensor` → `data.sensorList[].airs[]`(`{type,value,level}`).
  MQTT 상태에는 센서 종류만 있고 값이 없으므로 공기질은 REST 로만. 60초 주기 권장.

### 에어원 제어 wire format
```
topic   = cmd/rc/v2/{model_code}/{physical_device_id}/remote/{command}   # command: status|power|change-mode
payload = { "clientId": <MQTT 접속 clientId>,      # 응답 짝맞춤 필수
            "sessionId": "<ms epoch>",
            "requestTopic": topic,
            "responseTopic": topic + "/res",
            "state": { "desired": {...} } }         # status 조회면 state 없음
body    = { "serviceCode": 300, "payload": payload }  # topic/responseTopic 의 '/' 만 '\/' 이스케이프
```

## 3. MQTT (상태 push 수신 전용, 발행 안 함)

- 브로커: `nskr-iot.naviensmartcontrol.com:443` (나비엔 자체 AWS IoT 도메인 — 기기 registry 의
  endpoint 가 아님. 그걸로 붙으면 SNI 불일치 403).
- 전송: MQTT over WebSocket + TLS, region `ap-northeast-2`, service `iotdevicegateway`.
- 인증: **SigV4 사전서명 WebSocket path** (`/mqtt` 에 AWS4-HMAC-SHA256 서명, X-Amz-Security-Token 부착).
- clientId: `{uuid4}-U{user_seq}`.
- 구독: `{home_seq}/mate/#`, `{home_seq}/airone/#` (QoS 0). 발행 없음 — 제어는 REST.
- 재접속 백오프: 5,15,30,60,120,300초.

### 상태 파서 분리 (봉투가 다름)
- 매트: shadow 토픽이 `/update/accepted` 로 끝나고 `payload.state.reported` 가 dict 일 때만 사용.
- 에어원: shadow 아님. `payload.reported` 에 `roomController|odu|airMonitor|idu` 중 하나라도 있어야 통과.
- 토픽에 `/airone/` 포함 → 에어원 파서, 아니면 매트 파서.
- **부분 응답이 오므로 상태는 깊은 병합(deep-merge)** — 덮어쓰기 금지.

### 초기 상태 강제 요청
shadow 는 변화가 있을 때만 오므로, 구독 직후 초기 상태를 요청해야 조작 전까지 빈 상태로 남지 않는다.
매트는 빈 제어(`{}`), 에어원은 `status` 명령. **꺼진 기기에는 보내지 않음.**

폴링: 기본 900초(재접속/기기목록 변화용), 에어원 공기질은 REST 라 300초.

---

## 4. 에어원 (환기·제습·청정) 데이터 모델 — 이 앱의 1차 대상

소스: `Properties.data.did.reported` → `roomController` / `odu` / `airMonitor`.

- `physical_device_id = roomController.deviceId` (토픽에 쓰임; 기기목록 deviceId 와 다를 수 있음)
- **세대:** `modelCode >= 1000` 만 지원(신형 V2.1). 미만은 봉투/값이 달라 건너뜀.
- **running(운전상태):** 1=운전(ON) / 2=정지(OFF) / 3=외출(AWAY) / 4=자동건조(끈 뒤 진행).
  `roomController.running` 우선, 없으면 `odu.running`.
- **자동건조 진행률:** running=4 일 때 `additionalData = {"type":4,"value":진행률}` (역순 카운트).
  상태 텍스트 오른쪽에 `자동건조 (90%)` 로 표시.
- **mode:** 4=환기, 5=배기, 6=요리, 8=청정, 9=제습, 10=환기제습, 12=자동, 15=환기(외기), 17=바이패스, 18=음압환기.
- **option:** 1=없음, 2=터보, 3=절전, 4=숙면. (option 1 또는 4 일 때만 풍량 별도)
- **airVolume(풍량):** 1=미풍, 2=약풍, 3=강풍, 4=자동. **서버 `supportedAirVolumes` 안의 값만** 사용.
- **목표 습도:** 제습(9)·환기제습(10) 만. `roomController.additionalData = {"type":1,"value":습도}`,
  범위는 서버 `additionalData` min/max, step 5. **모드 전환 시 습도를 함께 실어야** 기기가 40% 로 초기화 안 됨.
- **오류:** `roomController.error.code` 또는 `odu.error.code`.
- **필터 사용률:** `odu.filter[i].usage.percent`.
- **공기질(REST):** pm1Dot0/pm2Dot5/pm10(㎍/㎥), co2(ppm), tvoc, radon(Bq/㎥), temperature(°C),
  humidity(%), total. 등급 0=알수없음/1=좋음/2=보통/3=나쁨/4=매우나쁨.

### 에어원 핵심 제어(최소 포팅 세트)
- 전원: cmd `power`, `roomController.running = 1/2`.
- 모드: cmd `change-mode`, `roomController.mode` + `option`.
- 풍량: cmd `change-mode`, `roomController.airVolume` (supportedAirVolumes 내).
- 습도: cmd `change-mode`, `roomController.additionalData={"type":1,"value":값}`.

---

## 5. 포팅 시 주의 (원 코드 주석 근거)

1. 상태는 **깊은 병합** (부분 응답).
2. **계정당 세션 1개** → 404 시 자동 재로그인.
3. AWS 자격증명 갱신은 **secured-sign-in 재호출**로만.
4. topic `/` → `\/` 이스케이프를 raw body 로 정확히 재현.
5. IoT 엔드포인트는 **나비엔 자체 도메인**.
6. **초기 상태 강제 요청** 필수.
7. 매트 shadow(`/update/accepted`+`state.reported`) 와 에어원(`payload.reported`) **파서 분리**.
8. 모델별 표를 코드에 박지 말 것 — 서버가 주는 제어방식/값범위를 따르고, 모르는 값은 건너뛰고 로그.
