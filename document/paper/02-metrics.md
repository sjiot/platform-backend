# 측정 지표

## 1. 지표 정의

### 네트워크

| 지표 | 정의 | 단위 |
|---|---|---|
| Mobius 쓰기 지연 | `create_cin` 요청 시작부터 응답까지 | ms |
| Mobius 읽기 지연 | Mobius GET 요청 시작부터 응답까지 | ms |
| 구독 전달 지연 | CIN 생성 시각부터 콜백 수신까지. 시계 차이 보정 후 사용 | ms |
| Redis 지연 | 명령 또는 파이프라인 1회 왕복 | ms |
| 콜백 처리 시간 | 콜백 핸들러 시작부터 응답 생성까지 | ms |
| 실패율 | 타임아웃과 비정상 응답의 비율 | % |

### 서버 부하

| 지표 | 정의 | 단위 |
|---|---|---|
| 요청당 Redis 왕복 수 | 요청 하나에서 발생한 Redis 왕복 수 | 회 |
| 주문당 CIN 수 | 주문 하나가 끝날 때까지 서버가 만든 CIN 수 | 건 |
| 중복 생략 수 | `push_process`가 CIN을 만들지 않은 횟수 | 회 |
| CIN 크기 | `con` JSON의 바이트 수 | byte |

### 주문 처리

| 지표 | 정의 | 단위 |
|---|---|---|
| 처리량 | 시간당 완료 주문 수 | 건/시간 |
| 대기 시간 | `queued`에서 `assigned`까지 | 초 |
| 단계 소요 시간 | 단계별 진입 시각의 차이 | 초 |
| 전체 완료 시간 | 첫 주문 접수부터 마지막 주문 완료까지 | 초 |
| 조립대 가동률 | 조립대가 주문을 잡고 있던 시간의 비율 | % |

평균 하나로 보고하지 않는다. 지연 시간은 p50, p95, 실패율을 함께 쓴다.

## 2. 지금 측정되는 것

`LATENCY_LOG=1`이면 `logs/latency/YYYY-MM-DD.jsonl`에 기록된다.

| `part` | 측정 위치 | 주의점 |
|---|---|---|
| `mobius_write` | `infra/mobius.py` `_timed_request` | 실패도 기록한다. 보낸 크기는 `sent_bytes`에 남는다 |
| `mobius_read` | `infra/mobius.py` `_timed_request` | |
| `mobius_subscribe` | `routes/callback.py` | 시계 차이와 워커 대기 시간이 섞여 있다 |
| `redis_read`, `redis_write` | `infra/extensions.py` | 파이프라인은 1건으로 기록한다 |
| `callback_handle` | `routes/callback.py` | 안쪽 Redis·Mobius 시간을 포함하므로 합산하지 않는다 |
| `http_request` | `infra/latency.py` | 콜백 외 HTTP 요청. 경로는 URL 규칙으로 묶는다 |
| `watchdog_expire` | `services/watchdog.py` | 마감 처리 1건 |
| `order_stage` | `routes/order.py`, `services/process.py` | 단계 진입 시각. 값은 `created_at`을 쓴다 |
| `process_push_skipped` | `services/process.py` | 중복으로 생략한 횟수 |

| 지표 | 명령 |
|---|---|
| 구간별 지연 | `python -m infra.latency_report` |
| 요청 종류별 Redis 왕복 수, CIN 수, 시간 구성 | `python -m infra.latency_report --by-request` |
| 주문 단계 사이 시간 | `python -m infra.latency_report --stages` |
| 대상별, 워커별 집계 | `--by-target`, `--by-worker` |
| 실험 묶음 지정 | `--run-id`, `--date` |
| 측정 환경 기록 | `python -m infra.latency_env --run-id <이름>` |
| 동시 콜백 부하 | `python -m infra.latency_burst` |

## 3. 아직 없는 측정

| 항목 | 필요한 이유 | 방법 |
|---|---|---|
| Mobius와 서버의 시계 차이 | `mobius_subscribe` 보정에 쓴다 | 테스트 전용 컨테이너를 정한 뒤 측정 스크립트 추가 |
| 마감 초과 후 처리까지 걸린 시간 | 1초 주기 감시의 감지 지연을 확인한다 | `services/watchdog.py` `_expire` |

### 구독 전달 지연 보정

`mobius_subscribe`는 CIN `rn`의 시각과 서버 수신 시각의 차이다. 다음 값이 함께 들어간다.

```text
측정값 = 실제 전달 시간 + Mobius와 서버의 시계 차이 ± 워커 대기 시간
```

1. 리눅스 서버에서 `chrony`로 시각을 맞추고 `chronyc tracking` 결과를 남긴다.
2. 테스트 전용 컨테이너에 CIN을 만들고, 응답의 `rn` 시각과 요청 전후 시각의 중간값을 비교해 시계 차이를 추정한다. 응답에 `rn`이 오는지 먼저 확인한다.
3. 서버가 한가할 때의 값을 전달 시간의 기준으로 삼는다.

시계 차이를 측정하지 못하면 절댓값을 쓰지 않고 조건 간 차이만 비교한다. `rn`이 ms 단위이고 공용 서버의 시계에 의존하므로 수 ms 차이는 해석하지 않는다.

## 4. 측정 부하 확인

같은 부하를 `LATENCY_LOG`를 끄고 켠 상태로 돌려 응답 시간을 비교한다. 파일 쓰기는 백그라운드 스레드가 맡지만 영향이 없다고 가정하지 않는다.

큐가 넘치면 기록이 버려지고 서버 로그에 `큐 초과`가 남는다. 이 경고가 나온 실험은 결과에서 제외한다.
