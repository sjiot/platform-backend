# 작업 내역

## 2026-11-12 주문 없는 창고 고장을 cnt_process warehouse_fault 로 보고

**수정 파일:** `services/device_fault.py`(신규), `services/warehouse_fault.py`(신규), `services/agv_fault.py`, `callbacks/warehouse.py`, `services/process.py`, `routes/order.py`, `routes/admin.py`, `services/recovery.py`, `config.py`, `tests/test_warehouse_fault.py`(신규, git 추적 제외)

### 변경 전

- 칸 고장(`disable`)은 진행 주문이 그 칸을 아직 써야 할 때만 주문 fault 로 올라갔다. 주문이 없거나, 이미 지나갔거나, 다른 칸이면 로그만 남고 cnt_process 에는 나가지 않았다.
- `idle`·`busy` 가 아닌 status 는 전부 disable/empty 갈래로 갔다. 창고 재기동 초기화 CIN(`status: null`)이 진행 주문에 `keycap_cartridge_failed` 를 걸었다 (flow-break-points 7번).

### 변경 후

| 칸 보고 | 진행 주문이 그 칸을 아직 써야 함 | 그 밖 |
|---|---|---|
| keycap `disable` (`keycap_cartridge_failed`) | 주문 fault (기존) | `warehouse_fault` — section `keycap_cartridge:keycap1:E_r` |
| keycap `disable` (`keycap_driver_failed`) | 주문 fault (기존) | `warehouse_fault` — section `keycap_cartridge:keycap1` (선반 8칸이 한 줄) |
| `empty`, board `disable` | 기존대로 | 올리지 않음 |
| `null`·모르는 status | 저장만, fault 없음 | 저장만 |

- cnt_process 에 `"warehouse_fault"` 를 늘 싣는다. 고장이 없으면 `null`, 있으면 `[{fault, fault_section, at}]`.
- 풀리는 경로 셋.
  - 창고가 그 칸에서 `idle`·`busy` 를 올림 (그 칸 + 그 선반 항목)
  - 관리자 `POST /admin/stock/revive` — 칸 되살림과 함께 (응답에 `warehouse_faults_cleared`)
  - 관리자 `POST /admin/warehouse/fault/clear` (body `password`, `fault_section` 선택) — 항목만 지운다. 칸 상태는 그대로. 고장 뒤 `empty` 가 와서 revive 가 409 인 경우용
- 새 주문 접수(`POST /order`) 409 는 `keycap_driver_failed`(선반 전체)가 걸려 있을 때만. `keycap_cartridge_failed`(칸 하나)는 접수를 막지 않고 그 칸을 쓰는 조합만 `blockers` 가 막는다. 배정은 어느 쪽도 막지 않는다.
- `GET /admin/faults` 에 `warehouse_fault` 추가. `/admin/reset` 이 `warehouse:faults` 를 지운다.
- `agv_fault` 와 저장·조회 코드를 `services/device_fault.py` 로 합쳤다. AGV 는 fault 이름, 창고는 fault_section 을 필드로 쓴다.

### 확인 결과

- 전체 테스트 통과 (가짜 Redis·Mobius 기준, 신규 19건 포함). 실제 장비 확인 전.
- 배포 직후 cnt_process 본문에 `warehouse_fault` 키가 생기므로 seq 가 한 번 오른다.

### 남은 일

- [ ] 창고 팀: cnt_process 의 `warehouse_fault` 형식 전달
- [ ] 관리자 페이지: `warehouse_fault` 표시, 항목별 revive 연결

## 2026-11-12 AGV fault 를 cnt_process 에 주문별 · 주문 없는 것으로 나눠 올림

**수정 파일:** `services/agv_fault.py`(신규), `callbacks/agv.py`, `services/process.py`, `services/dispatch.py`, `routes/order.py`, `routes/admin.py`, `services/recovery.py`, `config.py`, `tests/test_agv_fault.py`(신규, git 추적 제외)

### 변경 전

- 고장 보고의 대상 주문을 `_current_order` 로 찾았다. 후보가 애매하면 아무 주문(`cands[0]`)이나 골라, AGV 가 이미 손을 뗀 `received` 주문에 `agv_broken` 이 붙을 수 있었다.
- 대상 주문을 못 찾은 고장 보고는 경고 로그만 남기고 버렸다. cnt_process 에도, 관리자 화면에도 나가지 않았다.
- 주문 fault 를 해제해도 `agv:status` 에 `load_failed` 등이 남았다.

### 변경 후

| cnt_agv status | fault | 대상 주문 있음 | 대상 주문 없음 |
|---|---|---|---|
| `load_failed` | `pickup_failed` | `orders.{id}.fault`, section `agv` | 최상위 `agv_fault` |
| `unload_failed` | `drop_failed` | 〃 | 〃 |
| `agv_broken` | `agv_broken` | 〃 | 〃 |
| `arrived` 오배송 | `drop_failed` | `orders.{id}.fault`, section `agv:station{sid}` (기존) | - |

- 대상 주문: con 의 `order_id` → `station_id` → 트레이를 든 단계(`pickup_reached`·`loaded`·`arrived`·`verified`)의 주문 1건. 이미 사라진 주문의 `order_id` 는 대상 없음으로 본다.
- `agv_fault` 는 cnt_process 에 늘 실린다. 고장이 없으면 `null`, 있으면 `[{fault, fault_section: "agv", at}]`. 해결은 주문 fault 와 같이 `null` 로 확인한다.
- 주문 없는 고장은 관리자 해제(`POST /admin/agv/fault/clear`, body `password`·`parked`)로만 풀린다. `parked` 보고로는 풀리지 않는다.
- 걸려 있는 동안 배정(`try_assign_next`)과 새 주문 접수(`POST /order` 409)를 막는다. 해제하면 배정을 한 번 다시 시도한다.
- `GET /admin/faults` 응답에 `agv_fault` 추가. `/admin/reset` 이 `agv:faults` 를 지운다.
- 주문의 AGV fault 를 해제하면 `agv:status` 의 고장 값도 지운다.

### 확인 결과

- 전체 테스트 통과 (가짜 Redis·Mobius 기준, 신규 16건 포함). 실제 장비 확인 전.
- 배포 직후 cnt_process 본문에 `agv_fault` 키가 생기므로 seq 가 한 번 오른다.

### 남은 일

- [ ] AGV 팀: cnt_process 의 `agv_fault` 형식 전달, `null` 이면 재개하도록 합의
- [ ] 관리자 페이지: `agv_fault` 표시와 해제 버튼 연결

## 2026-11-11 가상 테스트 전 흐름 단절 지점 정리 · 1·3·6 수정

**수정 파일:** `services/process.py`, `callbacks/agv.py`, `services/archive.py`, `routes/station.py`, `document/flow-break-points.md`(신규), `tests/test_flow_breaks.py`(신규, git 추적 제외)

### 변경 전

- cnt_process 전송이 실패해도 지문(`process:last`)이 먼저 저장돼 같은 내용의 다음 push 가 생략됐다. 배정 push 실패 → 롤백 → 재배정이 CIN 없이 성공 처리됐다.
- `order_id`·`station_id` 없는 `discarded` 는 트레이를 든 단계의 주문을 골라 그 조립대를 풀었다. `release_station_after_discard` 는 주문 stage 를 확인하지 않았다.
- `unclaimed` 주문이 `GET /station/<sid>/status` 에 `ready: true` 로 나가고, `POST /station/<sid>/complete` 가 `unclaimed` 를 받아 폐기 전에 조립대를 풀었다.

### 변경 후

| 지점 | 처리 |
|---|---|
| `push_process` | `create_cin` 성공 뒤에만 `process:last` 저장. 실패하면 다음 push 가 같은 내용을 재전송 (seq 는 하나 건너뜀) |
| `discarded` | 대상은 `order_id` → `station_id` → 조립대의 `unclaimed` 주문 하나. `release_station_after_discard` 는 stage 가 `unclaimed` 일 때만 푼다 |
| `finish_order` | 이미 `unclaimed`·`completed` 인 주문은 무시 |
| `GET /station/<sid>/status` | `unclaimed`·`completed` 는 `ready: false` 400 |
| `POST /station/<sid>/complete` | `unclaimed`·`completed` 는 409 |

문제 전체 목록과 실기록 근거는 [flow-break-points.md](flow-break-points.md).

### 확인 결과

- 전체 테스트 통과 (가짜 Redis·Mobius 기준, 신규 9건 포함). 실제 장비 확인 전.

### 남은 일

- [ ] AGV 팀: `cnt_agv` con 샘플 (status 어휘, `order_id`·`station_id` 포함 여부) — flow-break-points 2번
- [ ] 창고 팀: `keycap_mismatched` 수신 시 재배출 범위 — 4번
- [ ] 프론트: 노쇼 뒤 조립대 조회 400 처리 확인 — 6번
- [ ] 5·7·9·10번은 별도 작업

## 2026-11-04 키캡 컨베이어 벨트 구간 구분

**수정 파일:** `callbacks/conveyor.py`, `tests/test_keycap_conveyor.py`(신규, git 추적 제외)

### 변경 전

- 키캡별 결과(`keycap1~4`)만 보고 처리했다.
- 보고에 구간 정보가 없으면 실패 구간을 모두 `01`로 기록했다. 02 벨트가 막혀도 `keycap_01_belt_jammed`로 표시됐다.

### 변경 후

`section01`, `section02` 값으로 진행 상황과 고장 구간을 구분한다.

```json
{"order_id": "ord_1234", "section01": "done"}
{"order_id": "ord_1234", "section02": "done"}
```

| 수신 값 | 처리 |
|---|---|
| `section01: done` | 이송 중, 단계 유지 |
| `section02: done` | `keycap_reached`로 전이 |
| `section01: failed` | `keycap_01_belt_jammed` fault, 위치 `keycap_conveyor:01` |
| `section02: failed` | `keycap_02_belt_jammed` fault, 위치 `keycap_conveyor:02` |

기존 `keycap1~4` 형식도 계속 받는다. 이 형식의 `failed`는 구간 정보가 없으면 이전처럼 `01`로 기록한다.

### 확인 결과

- 전체 테스트 통과 (가짜 Redis·Mobius 기준, 실제 장비 확인 전)
- 커밋·배포 전

### 남은 일

- [ ] 창고팀에 실제 형식 확인: 최근 12건은 모두 `keycap1~4` 형식이고, 마지막 보고는 2026-10-08이라 `section` 형식은 실제 데이터로 확인하지 못했다.
- [ ] 창고팀에 도착 기준 확인: `section02: done`을 키캡 도착으로 볼지
- [ ] 전환 확인 후 기존 `keycap1~4` 형식 삭제
- [ ] 「Mobius CNT · CIN 구조 정리」 3-2 형식 설명 수정
