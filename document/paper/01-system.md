# 시스템 구성

## 전체 구조

```text
Flutter 키오스크 ── HTTP ──▶ Flask 서버 ◀── 조회/갱신 ──▶ Redis
                               │  ▲
                     CIN 생성  │  │ 구독 알림 (콜백)
                               ▼  │
                             Mobius ◀──▶ 창고·컨베이어·로봇팔·AGV
```

| 구성 요소 | 역할 |
|---|---|
| Flask 서버 | 주문 접수, 배정, 단계 전이, 고장 감지 |
| Redis | 현재 상태의 단일 진실 소스 |
| Mobius (oneM2M) | 장비와 서버 사이의 메시지 전달과 기록 |
| 장비 | `cnt_process`를 구독해 움직이고 결과를 CIN으로 보고 |

서버는 리눅스 로컬 서버에서 돌고, Mobius는 외부 서버에 있다. 현재 `.env`의 Redis 주소는 사설 또는 루프백 대역이다. 리눅스 서버의 실제 구성은 [04-experiments.md](04-experiments.md) 1장에 기록한다.

## 명령과 보고 경로

1. 서버가 진행 중 주문 전체를 스냅샷으로 만들어 `cnt_process`에 올린다.
2. 장비가 구독으로 스냅샷을 받아 동작한다.
3. 장비가 결과를 자기 컨테이너에 CIN으로 올린다.
4. Mobius가 `/mobius/callback/<cnt>`로 서버에 알린다.
5. 서버가 Redis를 갱신하고 다음 단계의 스냅샷을 올린다.

**논문에서 볼 지점:** 콜백 한 건을 처리하는 동안 Redis 왕복과 Mobius 호출이 같은 요청 안에서 순서대로 일어난다. 그래서 Mobius 호출이 느리면 콜백 응답도 함께 늦어진다.

## 주문 단계

```text
queued → assigned → keycap_dispensed → keycap_reached → keycap_packed
       → tray_reached → board_packed → pickup_reached → loaded → arrived
       → verified → received → completed
```

곁가지 단계는 `keycap_mismatched`, `board_mismatched`, `unclaimed`이다. 전체 목록은 `config.py`의 `STAGE_ORDER`에 있다.

## 설계 요소

논문에서 알고리즘 기여로 설명할 수 있는 부분이다.

| 설계 | 내용 | 코드 |
|---|---|---|
| 배정 게이트 | 창고 유휴, 빈 조립대, 부품 상태를 확인해 배정한다. AGV는 게이트에서 뺀다 | `services/dispatch.py` |
| 막힌 주문 건너뛰기 | 앞 주문이 부품 때문에 막히면 뒤 주문을 먼저 배정한다 | `SKIP_BLOCKED` |
| 전량 스냅샷과 중복 생략 | 전체 상태를 보내되 내용이 같으면 CIN을 만들지 않는다 | `push_process` |
| 목록 소진형 취합 | 받은 개수 대신 남은 기대 목록을 세어 부분 재출고를 처리한다 | `services/collector.py` |
| 마감 기반 고장 감지 | Redis ZSET에 단계별 마감을 넣고 1초마다 만료 건을 찾는다 | `services/watchdog.py` |

## 규모

| 항목 | 값 | 근거 |
|---|---|---|
| 조립대 | 3대 | `dispatch.STATIONS` |
| AGV | 1대 | `dispatch.py` 주석 |
| 동시 창고 작업 | 1주문 | `warehouse:occupancy` 게이트 |
| 최대 대기열 | 3건 | `config.MAX_QUEUE_LEN` |
| 재고 컨테이너 | 36개 (키캡 32 + 보드 4) | 글자 8 × 색 4, 보드 4 |

대기열이 3건으로 작아서 시간 복잡도 개선은 논문의 중심 주제로 적합하지 않다.
