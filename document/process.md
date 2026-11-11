# 작업 내역

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
