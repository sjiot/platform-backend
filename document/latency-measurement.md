# 느린 구간 찾는 방법

- 작성: 2026-11-04
- 확인 범위: 맥에서 가짜 Mobius와 Redis DB 15로 확인했다. 실제 Mobius와 리눅스 서버에서는 아직 측정하지 않았다. `iowait`, 연결 대기열은 리눅스에서만 기록되며, 대기열 읽기는 예시 데이터로만 시험했다.

## 요약

네트워크 측정만으로는 느린 구간을 다 찾을 수 없다. 요청 시간을 나누고, 그 시간의 CPU와 메모리를 같은 파일에 함께 기록한다.

```text
서버에 도착하기까지 걸린 시간 = 네트워크 전달 + 앞 요청을 기다린 시간
서버 처리 시간(callback_handle) = Redis + Mobius + 코드 자체
```

| 구간 | 확인 방법 | 시간이 크면 볼 곳 |
|---|---|---|
| Redis | `redis_read`, `redis_write`, `metrics.redis_ms` | 호출 횟수, Redis 위치 |
| Mobius | `mobius_read`, `mobius_write`, `metrics.mobius_ms` | 호출 횟수, 연결 재사용, Mobius 응답 |
| 코드 자체 | `metrics.code_ms` | 계산, JSON 처리, 로그. CPU 프로파일러로 확인 |
| 요청 CPU | `metrics.cpu_ms` | 요청 스레드가 실제로 쓴 CPU |
| 대기 | 워커 사용률, 연결 대기열, `mobius_subscribe` 변화, `latency_burst` | 요청 1건 처리 시간, 워커 수 |
| PC 자원 | `system` 기록 | CPU 포화, 메모리 부족, 스왑 |

| 값 | 정확도 |
|---|---|
| Redis, Mobius, 코드, CPU | 요청 1건 단위로 잰다 |
| 메모리 | 프로세스 전체 RSS의 전후 차이다. 요청별 사용량이 아니라 증가 추세로 본다 |
| 대기 | 앱은 요청을 받기 전 시간을 모른다. 대기열 수와 사용률로 추정한다 |

## 1. 리눅스 PC 준비

```bash
pip install -r requirements.txt                      # psutil 포함
timedatectl                                          # 시간대 Asia/Seoul, NTP 동기화 확인
sudo timedatectl set-timezone Asia/Seoul             # 시간대가 다를 때만
df -h .                                              # 기록을 쌓을 디스크 여유 확인
```

- `mobius_subscribe`는 PC 시계에 영향을 받으므로 NTP 동기화가 필요하다.
- `gunicorn`에 `--preload`를 쓰지 않는다. 쓰면 워커에서 측정 스레드와 `watchdog-sweeper`가 돌지 않는다.
- 포트를 8080이 아닌 값으로 바꾸면 `LATENCY_PORT`도 같이 바꾼다. 다르면 연결 대기열이 비어 있게 된다.

## 2. 측정 켜기

실험을 시작할 때 환경을 먼저 남긴다. 결과는 `logs/latency/env-<run-id>.json`에 저장된다.

```bash
python -m infra.latency_env --run-id before --gunicorn "-w 1"
```

```bash
LATENCY_LOG=1 LATENCY_RUN_ID=before LATENCY_PORT=8080 gunicorn -w 1 -b 0.0.0.0:8080 'app:create_app()'
```

기록은 `logs/latency/YYYY-MM-DD.jsonl`에 쌓인다. `LATENCY_RUN_ID`는 개선 전후를 구분하는 이름이다. 맥에서 켜고 끈 결과 콜백 1건당 약 0.9ms가 늘었으므로 측정할 때만 켠다.

### systemd로 돌릴 때

`sudo systemctl edit <서비스 이름>`으로 아래 값을 넣고 재시작한다. 서비스 실행 계정이 저장 폴더에 쓸 수 있어야 한다.

```ini
[Service]
Environment=LATENCY_LOG=1
Environment=LATENCY_RUN_ID=before
Environment=LATENCY_PORT=8080
Environment=LATENCY_DIR=/var/log/sjiot/latency
```

```bash
sudo mkdir -p /var/log/sjiot/latency
sudo chown <서비스 계정> /var/log/sjiot/latency
sudo systemctl restart <서비스 이름>
LATENCY_DIR=/var/log/sjiot/latency python -m infra.latency_report --follow --by-request --system
```

Docker로 돌리면 저장 폴더를 볼륨으로 연결한다. 컨테이너의 PC CPU와 메모리 값은 컨테이너 제한이 아니라 PC 전체 기준일 수 있다.

### 기록 정리

```bash
du -sh logs/latency
find logs/latency -name '*.jsonl' -mtime +7 -exec gzip {} \;      # 7일 지난 기록 압축
find logs/latency -name '*.jsonl.gz' -mtime +30 -delete            # 30일 지난 기록 삭제
```

실험에 쓴 기록은 지우기 전에 다른 곳에 복사한다.

| 환경변수 | 설명 | 기본값 |
|---|---|---|
| `LATENCY_LOG` | `1`이면 기록 | 꺼짐 |
| `LATENCY_RUN_ID` | 측정 묶음 이름 | 없음 |
| `LATENCY_DIR` | 저장 폴더 | `logs/latency` |
| `LATENCY_RN_TZ` | CIN `rn`의 시간대 | `Asia/Seoul` |
| `LATENCY_SAMPLE_SEC` | PC 상태 기록 주기(초), `0`이면 끔 | `5` |
| `LATENCY_PORT` | 연결 대기열을 볼 gunicorn 포트 | `8080` |

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
| `system` | PC와 워커 상태. 워커별로 `LATENCY_SAMPLE_SEC`마다 1건. `ms`는 항상 0 |

### 기록 예시

요청 요약은 `callback_handle`, `http_request`, `watchdog_expire`에 남는다.

```json
{"created_at":"2026-11-04T14:19:51.228+09:00","part":"callback_handle","ms":111.243,"ok":true,"trace_id":"8431535d","pid":95337,"thread":"MainThread","note":{"cnt":"cnt_warehouse/cnt_keycap_status/cnt_J_b","status":200},"metrics":{"redis_ms":2.574,"mobius_ms":106.791,"code_ms":1.878,"cpu_ms":4.564,"rss_before_mb":64.61,"rss_after_mb":64.61}}
```

| `metrics` | 의미 |
|---|---|
| `redis_ms`, `mobius_ms` | 이 요청의 호출 시간 합 |
| `code_ms` | 처리 시간 - Redis - Mobius |
| `cpu_ms` | 요청 스레드의 CPU 시간. Redis, Mobius 호출 중 쓴 CPU도 포함 |
| `rss_before_mb`, `rss_after_mb` | 요청 전후 워커 메모리 |

| `system`의 `metrics` | 의미 |
|---|---|
| `worker_busy_pct` | 기록 주기 중 요청을 처리한 시간 비율 |
| `accept_queue` | 워커가 받아 가길 기다리는 연결 수(리눅스) |
| `cpu_pct`, `iowait_pct` | PC CPU 사용률, 디스크 등 입출력 대기(`iowait`는 리눅스) |
| `load_1m`, `cpu_count` | 1분 부하, 코어 수 |
| `mem_available_mb` | 사용 가능한 메모리 |
| `swap_used_mb`, `swap_in_mb`, `swap_out_mb` | 스왑 사용량, 기록 주기 중 입출력량 |
| `worker_rss_mb`, `worker_cpu_pct` | 워커 메모리와 CPU 사용률 |

Mobius 호출 기록에는 그 호출의 `cpu_ms`도 남는다. Redis 호출은 0.1ms 수준이라 CPU를 호출마다 재지 않는다.

- `callback_handle`, `http_request`, `watchdog_expire`에는 같은 요청의 Redis와 Mobius 시간이 들어 있다. 파트별 값을 모두 더하면 안 된다.
- `cpu_ms`와 `code_ms`도 더하면 안 된다. 겹치는 시간이 있다.
- `mobius_subscribe`에는 두 서버의 시계 차이가 그대로 들어간다. 약 32,400,000ms로 나오면 `LATENCY_RN_TZ=UTC`로 바꾼다.
- 장비가 Mobius에 올리는 시간은 측정되지 않는다.

## 3. 찾는 순서

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
python -m infra.latency_report --follow --window 60 --by-request --system
python -m infra.latency_report --follow --window 60 --by-target --by-worker
```

### 2단계: 느린 요청을 나눠 본다

요청 종류별 중앙값과 가장 느린 요청을 나눠 본다.

```bash
python -m infra.latency_report --date 2026-11-04 --run-id before --by-request --slow 10
```

| 결과 | 다음 할 일 |
|---|---|
| Mobius가 가장 큼 | 연결 재사용, 요청당 CIN 수, 기록용 CIN 분리 |
| Redis가 가장 큼 | 호출 횟수를 줄이거나 파이프라인으로 묶기, Redis 위치 확인 |
| 코드와 CPU가 모두 큼 | 3단계 CPU 프로파일 |
| 코드만 크고 CPU는 작음 | 로그 쓰기, CPU 경쟁, 스왑 의심. 5단계 확인 |
| RSS증가가 큰 요청 | 큰 데이터 조회와 캐시 확인 |

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

먼저 워커 사용률과 연결 대기열을 본다.

```bash
python -m infra.latency_report --date 2026-11-04 --run-id before --system
ss -ltn 'sport = :8080'                              # Recv-Q가 현재 연결 대기 수
```

| 워커 사용률 | 연결 대기열 | 해석 |
|---|---|---|
| 낮음 | 0 근처 | 대기 없음. 지연은 처리나 네트워크 쪽 |
| 100% 근처 | 0보다 큼 | 대기 발생. 처리 시간을 줄이거나 동시 처리를 늘림 |
| 100% 근처 | 0 근처 | 한계에 가까움 |

대기 시간은 대략 `대기 연결 수 × 요청 처리 p50`으로 어림한다. 기록 주기 사이에 잠깐 쌓였다 빠진 대기는 놓칠 수 있다.

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

측정 기록의 `system`에서 측정 중 상태를 먼저 본다. 지금 상태를 직접 볼 때는 아래 명령을 쓴다.

```bash
python -m infra.latency_report --date 2026-11-04 --run-id before --system
top                                                  # CPU 사용률, load average
ps -eo pid,rss,cmd --sort=-rss | head -15            # 메모리를 많이 쓰는 프로세스
free -h                                              # 사용 가능한 메모리
vmstat 1 5                                           # si, so가 계속 0이 아니면 스왑 발생
```

- CPU가 코어 수만큼 계속 차 있으면 계산이 병목이다.
- CPU가 낮은데 느리면 네트워크나 외부 응답을 기다리는 경우가 많다.
- `iowait`가 높으면 디스크 등 입출력을 기다리는 시간이 길다.
- 스왑 입출력이 계속 있으면 메모리 부족이다.
- 워커 RSS가 처음보다 계속 늘면 메모리 누수나 제한 없는 캐시를 의심한다.
- 워커가 여럿이면 PC 상태 기록이 워커마다 남는다. CPU와 메모리 값은 PC 전체 값이므로 워커별로 더하지 않는다.

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

## 4. 개선 전후 비교

1. `LATENCY_RUN_ID=before`로 측정한다.
2. 한 번에 하나만 고친다.
3. `LATENCY_RUN_ID=after`로 같은 흐름을 다시 측정한다.
4. 같은 파트의 `p50`, `p95`, `합계`, `실패`와 코드, CPU, 메모리를 비교한다.

```bash
python -m infra.latency_report --date 2026-11-04 --run-id before --by-target --by-request --system
python -m infra.latency_report --date 2026-11-04 --run-id after --by-target --by-request --system
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
- `psutil`이 없어도 서버와 시간 측정은 돌지만 CPU와 메모리 값은 기록되지 않는다.
