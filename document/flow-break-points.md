# 정상 흐름 단절 지점 — 가상 테스트 전 점검

- 작성: 2026-11-11
- 기준 코드: `lgk-refactor` 브랜치 `d140d71`
- 확인 방법: 코드 추적 + 2026-11-04 리눅스 서버 지연 로그 대조
  (`~/coshow/backend/logs/latency/2026-11-04.jsonl`, 맥 사본 `logs/linux-server/latency/`). 가상 실행으로 재현하지는 않았다.
- 범위: 정상 흐름 = 정의된 실패 흐름(노쇼·fault 해제·재출고·중단·폐기)까지.
  대기열 확장·동시성 계획은 [queue-expansion.md](queue-expansion.md), 콜백 문서 불일치는 [mobius-callback-mismatch.md](mobius-callback-mismatch.md).
- 테스트 현황: `pytest` 105건 통과. 다만 창고 구간·fault 해제·중단만 다루고 **AGV 전이, QR 인증, 노쇼, 폐기 대기는 테스트가 없다.**

## 요약

| # | 항목 | 심각도 | 파일 | 상태 |
|---|---|---|---|---|
| 1 | cnt_process 전송 실패 뒤 같은 내용이 재전송되지 않는다 | 높음 | `services/process.py` | **수정** (2026-11-11) |
| 2 | AGV 보고 어휘·형식이 코드와 맞는지 확인된 바 없다. 실기 전이 0건 | 높음 | `callbacks/agv.py` | AGV 팀 확인 필요 |
| 3 | 주문 정보 없는 `discarded` 가 다른 조립대를 푼다 | 높음 | `callbacks/agv.py`, `services/archive.py` | **수정** (2026-11-11) |
| 4 | `keycap_mismatched` 재출고 명령에 부족분이 없다 | 높음 | `services/process.py`, `callbacks/warehouse.py` | 창고 팀 확인 필요 |
| 5 | 다중 워커에서 옛 스냅샷이 더 큰 seq 로 나갈 수 있다 | 중간 (운영) | `services/process.py` | 미수정 |
| 6 | 노쇼 뒤 조립대 조회가 ready 이고, 완료 버튼이 폐기 전에 조립대를 푼다 | 중간 | `routes/station.py`, `services/archive.py` | **수정** (2026-11-11) |
| 7 | 창고 초기화 CIN(status null)이 진행 주문에 fault 를 건다 | 중간 | `callbacks/warehouse.py` | 미수정 |
| 8 | 새 PC 첫 기동 순서가 틀리면 아무것도 배정되지 않는다 | 중간 | `services/stock.py`, `routes/admin.py`, `services/line.py` | 절차로 흡수 |
| 9 | 문서화된 콜백 불일치 4건 | 중간 | `callbacks/conveyor.py`, `callbacks/agv.py` | 미수정 |
| 10 | 가상 테스트 자체를 방해하는 것 — 120초 마감, discovery 순서, snapshot 500 | 낮음 | `services/watchdog.py`, `infra/mobius.py`, `services/snapshot.py` | 확인 필요 |

## 11/04 실기록이 말해주는 것

| 항목 | 값 |
|---|---|
| 접수 주문 | 8건 (`POST /order` 200 ×8, 409 ×2, 400 ×1) |
| 창고 구간을 끝까지 간 주문 | 1건 — `ord_c3846dbb`, 배정 19:12:23 → pickup_reached 19:14:05 (102초) |
| 그 주문의 AGV 구간 | `cnt_agv` 콜백 11건 수신(처리 1~6ms, 예외 0건), **stage 전이 0건**. 300초 뒤 워치독이 completed 로 종료 |
| 나머지 7건 | assigned 에서 120초 마감 → fault. 관리자 abort 7회, fault 해제 1회, reset 3회 |
| `GET /admin/debug/snapshot` | 5회 모두 500 |

콜백 수신 분포: keycap_status 93, keycap_dispense 28, keycap_robot_arm 24, tray_conveyor 15, board_status 14, cnt_agv 11, tray_robot_arm 9, keycap_conveyor 6, board_robot_arm 5, board_dispense 4.

읽는 법.
- 창고·컨베이어·로봇팔 보고 형식은 한 번은 코드와 맞았다 (1건이 pickup_reached 까지 갔다). 가상 테스트는 **pickup_reached 이후**에 집중하는 편이 효율적이다.
- AGV 보고는 파싱도 되고 예외도 없었는데 전이를 하나도 못 만들었다. status 어휘나 con 형태가 `TRANSITIONS` 와 다르다는 뜻이다 (2번).
- 확인 도구인 snapshot 이 먼저 고장나 있다 (10번).

## 1. cnt_process 전송 실패 뒤 재전송 불가 — 수정

**문제 코드:** `services/process.py::push_process`

| 구분 | 내용 |
|---|---|
| 변경 전 | 지문(`process:last`)을 `create_cin` **전에** 저장했다. 전송이 실패해도 '머신이 봤다'로 남아, 같은 내용의 다음 push 는 "변화 없음"으로 건너뛴다. |
| 발생 상황 | 배정 push 실패 → `_rollback` → 다음 트리거에서 같은 주문·같은 조립대로 재배정 → 내용이 같아 push 생략 → 배정은 성공 처리, 창고는 주문을 모른다 → 120초 뒤 `stage_timeout`. 그 뒤 fault 해제 push 가 나가서야 창고가 받는다. 다른 단계의 전송 실패도 같은 식으로 그 상태를 잃는다(`verified` 유실 → AGV 가 하차하지 않음). |
| 변경 후 | 지문은 `create_cin` 이 성공한 뒤에만 저장한다. 실패하면 다음 push 가 같은 내용을 다시 보낸다. seq 는 이미 올라갔으므로 재전송은 번호 하나를 건너뛰고 나간다 — 머신의 'seq 안 오르면 무시' 필터는 빈 번호를 상관하지 않는다. 그 사이 snapshot 의 "Redis seq 가 Mobius 보다 앞선다" 경고가 정확히 이 상태를 가리킨다. |

## 2. AGV 보고 어휘·형식 미확인

**문제 코드:** `callbacks/agv.py:12-22` (`TRANSITIONS`, `FAULTS`)

코드가 받는 status 는 `loaded` `arrived` `unloaded` `parked` `discarded` `load_failed` `unload_failed` `agv_broken` 뿐이다. 그런데 `config.py:8` 과 `services/dispatch.py:11` 주석은 `station_arrived` 를 언급한다. 11/04 에 AGV 보고 11건이 전부 "미정의 status" 또는 "대상 주문을 못 찾음"으로 떨어진 것과 맞물린다.

함께 확인할 것.

| 항목 | 코드의 가정 | 틀리면 |
|---|---|---|
| status 어휘 | 위 8개 | 전이 없음 → `loaded` 120초 · `pickup_reached` 300초 마감 |
| con 형태 | 최상위 `status`, `order_id`, `station_id`, `coord` | `status` 가 한 겹 안에 있으면 전부 미정의 |
| `unloaded` 시점 | `verified` 뒤에만 받는다 (`TRANSITIONS["unloaded"]`) | AGV 가 QR 인증 전에 내려놓으면 `verified` 에서 120초 마감 |
| `arrived` 의 `station_id` | 오배송 판정에 쓴다 | 없으면 오배송을 못 잡는다 (9번) |

**확인 방법:** 서버 pm2 로그의 `[callback:cnt_agv] con=` 줄에 11/04 원문이 남아 있다. AGV 팀에 con 샘플을 받는 것이 가장 빠르다.

## 3. 주문 정보 없는 `discarded` 가 다른 조립대를 푼다 — 수정

**문제 코드:** `callbacks/agv.py::on_agv` 의 discarded 분기, `services/archive.py::release_station_after_discard`

| 구분 | 내용 |
|---|---|
| 변경 전 | `order_id` 가 없으면 `_current_order` 폴백을 탔다. 그 폴백은 '트레이를 들고 있는 단계'(`AGV_HOLDING`)의 주문을 고르는데, 폐기 중인 주문은 `unclaimed` 라 그 집합에 없다. `release_station_after_discard` 는 주문 stage 를 보지 않고, 마감 해제(`disarm`)를 검사보다 먼저 했다. |
| 발생 상황 | 조립대 1 이 노쇼(`unclaimed`, 폐기 대기), 조립대 2 가 다음 주문 `pickup_reached`. AGV 가 `{"status": "discarded"}` 만 보낸다 → 조립대 2 의 주문이 뽑힌다 → 그 조립대가 풀리고, 주문 해시 TTL 이 10분으로 줄고, cnt_process 에서 사라진다 → AGV 는 픽업 명령을 잃고 조립대 2 는 이중 배정된다. `order_id` 를 잘못 실어도 같은 일이 난다. |
| 변경 후 | 폐기 대상은 `order_id` → `station_id` → 조립대에서 `unclaimed` 인 주문 하나(`_discard_target`). 둘 이상이면 모호로 버리고 로그를 남긴다. `release_station_after_discard` 는 stage 가 `unclaimed` 일 때만 풀고, `disarm` 도 그 검사 뒤에 한다. |

## 4. `keycap_mismatched` 재출고 명령에 부족분이 없다

**문제 코드:** `services/process.py::_build_body`, `callbacks/warehouse.py::_mismatch`

| 구분 | 내용 |
|---|---|
| 문서 | 재출고는 부족분만 내려보낸다 (명세 8-2, `services/collector.py` 머리말). |
| 코드 | 스냅샷은 `status` 만 `keycap_mismatched` 로 바꾼다. `collector.remaining` 은 로그에만 쓰이고 cnt_process 에 실리지 않는다. |

**발생 상황** — 창고가 `keycap_mismatched` 를 받고 4개를 다시 낸다고 가정하면

1. 부족분이 `[E_r]` 인데 창고가 `N_r` 부터 낸다.
2. `consume` 이 pending 에서 못 지워 `unexpected` → `_mismatch` 2회차 → 다시 `keycap_mismatched`.
3. 한 번 더 반복하면 `MAX_RETRY` 초과 → `keycap_cartridge_failed` fault.

부족분이 먼저 나오면 `keycap_dispensed` 로 넘어가지만 나머지 3개가 트레이에 더 실린다. 어느 쪽이든 정의된 재출고 흐름이 아니다.

**수정 방향 (창고 팀 확인 뒤):** 스냅샷 주문 항목에 `keycap_pending`(= `collector.remaining`)을 싣고, 창고는 그 목록만 재배출한다. 목록이 바뀌면 지문도 바뀌므로 `push_process` 의 중복 생략과 충돌하지 않는다.

## 5. 다중 워커에서 옛 스냅샷이 더 큰 seq 로 나간다

**문제 코드:** `services/process.py::push_process`

본문 생성 → 지문 비교 → seq 증가 → 전송이 락 없이 이어진다. 서버는 gunicorn 워커 4개 × 스레드 8개다 (2026-11-04 변경).

**발생 상황**

1. 마지막 배출 콜백(스레드 B)과 그 카트리지의 `idle` 상태 콜백(스레드 A)이 거의 동시에 온다.
2. A 가 `assigned` 로 본문을 만든다.
3. B 가 `keycap_dispensed` 로 전이하고 seq N+1 을 올린다.
4. A 의 지문은 `process:last` 와 다르므로 seq N+2 로 **`assigned` 스냅샷**을 올린다.
5. 창고는 N+2 를 최신으로 보고 assigned 로 되돌아간다 — 재배출 가능.

단일 워커 가상 테스트에서는 재현되지 않는다. 수정은 [queue-expansion.md](queue-expansion.md) 1단계(배정 락)와 묶어 `push_process` 전체를 Redis `SET NX` 락으로 감싸는 것이다.

## 6. 노쇼 뒤 조립대 화면이 주문을 계속 받는다 — 수정

**문제 코드:** `routes/station.py` 의 `/station/<sid>/status`, `/station/<sid>/complete`, `services/archive.py::finish_order`

| 구분 | 내용 |
|---|---|
| 변경 전 | 조회는 `arrived` 이전만 걸러서 `unclaimed` 주문이 최대 300초(폐기 마감) 동안 `ready: true` 로 나갔다. 완료 버튼은 `received` 뒤만 막는데 `unclaimed` 는 `STAGE_ORDER` 에서 `received` 앞이라 통과했다 → `finish_order(completed)` → 조립대가 폐기 전에 풀린다. 조립대를 잡아 두는 이유는 `services/archive.py::_release_resources` 주석에 있다 (폐기 명령이 스냅샷에서 사라진다). |
| 변경 후 | 조회는 `unclaimed`·`completed` 를 `ready: false` 400 으로 내린다(프론트가 400 을 '배정 없음'으로 이미 처리한다). 완료는 같은 두 stage 를 409 로 막는다. `finish_order` 도 이미 끝난 주문이면 무시한다 — 조립 타임아웃 경로(`_complete_station`)까지 같이 막힌다. |

프론트 확인: 노쇼 처리 뒤 디스플레이가 400 을 받으면 대기 화면으로 돌아가는지.

## 7. 창고 초기화 CIN 이 진행 주문에 fault 를 건다

**문제 코드:** `callbacks/warehouse.py::on_cartridge_status` (`:196-215`, `:244-267`)

`status` 가 `idle`·`busy` 가 아니면 전부 disable/empty 갈래로 간다. 창고 재기동 때 오는 초기화 CIN 은 `status: null` 이다 (2026-11-04 서버 확인). 진행 주문이 `assigned` 이고 그 칸을 쓰면 `keycap_cartridge_failed` 가 붙는다. 보드 칸은 fault 없이 "보관대 이상" 로그만 남는다.

**발생 상황:** fault 를 수리하고 창고 프로그램을 재시작하는 바로 그 흐름이다. 36건이 쏟아지며 주문의 키캡 칸 4개마다 fault 가 덮어써진다.

**수정 방향:** 모르는 status(null 포함)는 저장만 하고 fault 갈래로 보내지 않는다. 불변식 10번(`services/invariants.py`)이 `null` 을 위반으로 잡으므로 저장 형식도 같이 정한다 (`unknown` 으로 정규화할지).

## 8. 새 PC 첫 기동 순서

Redis 와 Mobius 가 모두 새것이면.

| 상태 | 결과 | 근거 |
|---|---|---|
| `warehouse:stock` 없음 | 모든 칸이 `unknown` 으로 막혀 배정이 안 된다 | `services/stock.py::blockers` |
| 같은 상태에서 채우기 | `POST /admin/stock/refill` 이 400 ("아직 재고 데이터가 없는 칸") | `routes/admin.py:824-826` |
| `line:status` 없음 | `POST /order` 503 | `services/line.py:11-13` |

**순서:** `POST /admin/reset` (mode zero) → 스냅샷 확인 → `POST /admin/line {"status": "open"}`. 그 전에 Mobius 리소스 트리(AE·cnt 44개·구독 nu)가 새 PC 주소로 만들어져 있어야 한다.

## 9. 문서화된 콜백 불일치 4건

[mobius-callback-mismatch.md](mobius-callback-mismatch.md) 의 4건이 그대로다. 이른 컨베이어 보고로 단계 건너뜀, 늦게 온 `failed` 로 fault, `order_id` 없는 `arrived` 의 오배송 미판정, fault 덮어쓰기. 2번의 AGV con 샘플이 오면 3번째 항목은 같이 결정된다.

## 10. 가상 테스트 자체를 방해하는 것

| 항목 | 내용 | 근거 |
|---|---|---|
| 120초 마감 | 창고 구간 마감이 대부분 120초라 사람이 장비 역할을 하면 fault 가 먼저 난다. 값이 하드코딩이라 env 로 못 늘린다 | `services/watchdog.py:11-25` |
| discovery 순서 | fault 해제·재기동 복구의 배출 CIN 대조는 "discovery 가 최신순" 이라는 COSS 플랫폼 실측에 기댄다. 새 로컬 Mobius 에서 순서를 다시 재야 한다. 반대면 해제 시 유실 배출을 못 잡고, 재기동 복구가 pending 을 4개로 되돌린다 | `infra/mobius.py::get_recent_cons`, `services/recovery.py::_restore_collector` |
| snapshot 500 | 11/04 서버에서 5회 모두 500. 원인 미확인 — 로컬 테스트는 통과한다. 의심 지점은 `services/snapshot.py::_mobius` 의 `con["orders"].items()` (옛 형식 CIN 이면 실패). pm2 로그의 traceback 확인 필요 | `routes/admin.py:518-578` |

## 확인·결정 필요

- [ ] AGV 팀: `cnt_agv` con 샘플 — status 어휘, `order_id`·`station_id` 포함 여부, `unloaded` 를 `verified` 뒤에만 보내는지 (2번)
- [ ] 창고 팀: `keycap_mismatched` 를 받으면 무엇을 재배출하는지 (4번)
- [ ] 프론트: 노쇼 뒤 `GET /station/<sid>/status` 400 처리 (6번)
- [ ] 서버 pm2 로그: `GET /admin/debug/snapshot` 500 traceback (10번)
- [ ] 새 로컬 Mobius 의 discovery 반환 순서 (10번)
- [ ] 5번 락과 7번 status 정규화는 별도 작업으로
