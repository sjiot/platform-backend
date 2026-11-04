# 느린 구간 찾는 방법

- 작성: 2026-11-04
- 확인 범위: 맥에서 가짜 Mobius와 Redis DB 15로 확인했다. 실제 Mobius와 리눅스 서버에서는 아직 측정하지 않았다.

## 요약

네트워크 측정만으로는 느린 구간을 다 찾을 수 없다. 요청 시간을 아래 4가지로 나눠서 본다.

```text
서버에 도착하기까지 걸린 시간 = 네트워크 전달 + 앞 요청을 기다린 시간
서버 처리 시간(callback_handle) = Redis + Mobius + 코드 자체
```

| 구간 | 확인 방법 | 시간이 크면 볼 곳 |
|---|---|---|
| Redis | `redis_read`, `redis_write` | 호출 횟수, Redis 위치 |
| Mobius | `mobius_read`, `mobius_write` | 호출 횟수, 연결 재사용, Mobius 응답 |
| 코드 자체 | 처리 시간 - Redis - Mobius | 계산, JSON 처리, 로그. CPU 프로파일러로 확인 |
| 대기 | `latency_burst.py`, `mobius_subscribe` 변화 | 요청 1건 처리 시간, 워커 수 |
| PC 자원 | `top`, `free`, `vmstat` | CPU 포화, 메모리 부족, 스왑 |

## 1. 측정 켜기

실험을 시작할 때 환경을 먼저 남긴다. 결과는 `logs/latency/env-<run-id>.json`에 저장된다.

```bash
python -m infra.latency_env --run-id before --gunicorn "-w 1"
```

```bash
LATENCY_LOG=1 LATENCY_RUN_ID=before gunicorn -w 1 -b 0.0.0.0:8080 'app:create_app()'
```

기록은 `logs/latency/YYYY-MM-DD.jsonl`에 쌓인다. `LATENCY_RUN_ID`는 개선 전후를 구분하는 이름이다. 켜면 콜백 1건당 약 0.8ms가 늘어나므로 측정할 때만 켠다.

| 환경변수 | 설명 | 기본값 |
|---|---|---|
| `LATENCY_LOG` | `1`이면 기록 | 꺼짐 |
| `LATENCY_RUN_ID` | 측정 묶음 이름 | 없음 |
| `LATENCY_DIR` | 저장 폴더 | `logs/latency` |
| `LATENCY_RN_TZ` | CIN `rn`의 시간대 | `Asia/Seoul` |

### 파트 의미

| `part` | 측정 내용 |
|---|---|
| `redis_write`, `redis_read` | Redis 명령 왕복 시간 |
| `mobius_write`, `mobius_read` | Mobius 요청 응답 시간 |
| `mobius_subscribe` | CIN 생성부터 콜백 도착까지 |
| `callback_handle` | 콜백 도착부터 응답까지 |
| `http_request` | 콜백 외 HTTP 요청의 시작부터 응답까지 |
| `watchdog_expire` | 워치독의 마감 처리 1건 |
| `order_stage` | 주문 단계 진입 시각. `ms`는 항상 0 |
| `process_push_skipped` | 내용이 같아 `cnt_process`를 생략한 사건. `ms`는 항상 0 |

- `callback_handle`, `http_request`, `watchdog_expire`에는 같은 요청의 Redis와 Mobius 시간이 들어 있다. 파트별 값을 모두 더하면 안 된다.
- `mobius_subscribe`에는 두 서버의 시계 차이가 그대로 들어간다. 약 32,400,000ms로 나오면 `LATENCY_RN_TZ=UTC`로 바꾼다.
- 장비가 Mobius에 올리는 시간은 측정되지 않는다.

## 2. 찾는 순서

### 1단계: 어느 파트가 느린지 본다

```bash
python -m infra.latency_report --date 2026-11-04 --run-id before --by-target
```

- `p50`: 평소 속도
- `p95`, `max`: 가끔 느려지는 정도
- `합계`: 전체 시간을 가장 많이 쓰는 대상
- `실패`: 오류와 타임아웃 건수

건당 시간이 짧아도 호출이 많으면 합계가 커지므로 `합계`와 `p95`가 큰 대상부터 본다. 실행 중 상태는 아래 명령으로 본다.

```bash
python -m infra.latency_report --follow --window 60 --by-target --by-worker
```

### 2단계: 느린 요청을 나눠 본다

요청 종류별로 Redis 왕복 수, CIN 수, Redis와 Mobius 시간을 나눠 본다.

```bash
python -m infra.latency_report --date 2026-11-04 --run-id before --by-request
```

코드 자체의 시간은 `p50 - Redis ms - Mobius ms`로 어림한다.

| 가장 큰 값 | 다음 할 일 |
|---|---|
| Mobius | 연결 재사용, 요청당 CIN 수, 기록용 CIN 분리 |
| Redis | 호출 횟수를 줄이거나 파이프라인으로 묶기, Redis 위치 확인 |
| 코드 | 3단계 CPU 프로파일 |

`trace_id`는 콜백, 일반 HTTP 요청, 워치독 마감 처리에 붙는다. 워치독의 주기 조회 같은 나머지 백그라운드 작업은 `--by-target`, `--by-worker` 집계로 본다.

주문 단계 사이의 시간은 아래 명령으로 본다. 단위는 초다.

```bash
python -m infra.latency_report --date 2026-11-04 --run-id before --stages
```

### 3단계: 코드 자체가 느리면 CPU를 본다

`py-spy`는 코드를 고치지 않고 실행 중인 워커를 볼 수 있다.

```bash
pip install py-spy
pgrep -af gunicorn                                  # 워커 pid 확인
sudo py-spy top --pid <워커 pid>                    # 실시간
sudo py-spy record --pid <워커 pid> -d 30 -o cpu.svg # 30초 기록
```

`cpu.svg`에서 폭이 넓은 함수가 시간을 많이 쓰는 곳이다.

### 4단계: 기다리는 시간을 본다

처리 시간은 그대로인데 전달 시간이 늘면 앞 요청을 기다리는 중일 수 있다.

| `callback_handle` | `mobius_subscribe` | 해석 |
|---|---|---|
| 그대로 | 커짐 | 대기 또는 네트워크 지연 |
| 커짐 | 커짐 | 처리가 느려져 대기까지 늘어남 |
| 커짐 | 그대로 | 처리만 느려짐, 2단계로 이동 |

대기만 따로 보려면 동시 요청 테스트를 돌린다. 가짜 Mobius와 Redis DB 15를 쓰므로 장비에 닿지 않는다. `--cin-ms`에는 실제로 잰 `mobius_write` p50을 넣는다.

```bash
python -m infra.latency_burst --workers 1 --concurrency 1 --requests 100 --cin-ms 150
python -m infra.latency_burst --workers 1 --concurrency 20 --requests 100 --cin-ms 150
```

### 5단계: PC 자원을 본다

```bash
top                                                  # CPU 사용률, load average
ps -eo pid,rss,cmd --sort=-rss | head -15            # 메모리를 많이 쓰는 프로세스
free -h                                              # 사용 가능한 메모리
vmstat 1 5                                           # si, so가 계속 0이 아니면 스왑 발생
```

- CPU가 코어 수만큼 계속 차 있으면 계산이 병목이다.
- CPU가 낮은데 느리면 네트워크나 외부 응답을 기다리는 경우가 많다.
- 스왑이 계속 발생하면 메모리 부족이다.

### 6단계: 네트워크 경로를 본다

Mobius가 느릴 때 원인이 연결인지 서버 응답인지 나눠 본다.

```bash
curl -o /dev/null -s -w 'DNS %{time_namelookup}  TCP %{time_connect}  TLS %{time_appconnect}  첫응답 %{time_starttransfer}  전체 %{time_total}\n' https://platform.iotcoss.ac.kr/
ping -c 20 platform.iotcoss.ac.kr
ip route get 1.1.1.1                                 # 유선/Wi-Fi 인터페이스 확인
```

- TLS가 크면 연결 재사용 효과가 크다.
- `ping`의 편차가 크면 Wi-Fi나 회선을 먼저 확인한다. 응답을 막아 둔 서버면 `ping`은 판단에 쓰지 않는다.
- `첫응답`은 경로 비용을 보는 값이다. 실제 쓰기 시간은 `mobius_write`로 본다.

## 3. 개선 전후 비교

1. `LATENCY_RUN_ID=before`로 측정한다.
2. 한 번에 하나만 고친다.
3. `LATENCY_RUN_ID=after`로 같은 흐름을 다시 측정한다.
4. 같은 파트의 `p50`, `p95`, `합계`, `실패`를 비교한다.

```bash
python -m infra.latency_report --date 2026-11-04 --run-id before --by-target
python -m infra.latency_report --date 2026-11-04 --run-id after --by-target
```

비교할 때는 주문 수, 워커 수, 네트워크 환경을 맞춘다. 측정 기록을 JSON 배열로 받으려면 아래 명령을 쓴다.

```bash
python -m infra.latency_report --date 2026-11-04 --json logs/latency/2026-11-04.json
```

## 주의

- 실제 `cnt_process`와 콜백 대상 컨테이너에 테스트 CIN을 쓰지 않는다. 장비나 백엔드 로직이 움직일 수 있다.
- 로컬 서버도 `.env`의 `MP_URL`이 실제 Mobius면 실제로 기록한다.
- 측정 기록을 Redis에 저장하지 않는다. Redis 측정값이 왜곡된다.
- 측정이 끝나면 `LATENCY_LOG`를 끄고 서버를 재시작한다.
