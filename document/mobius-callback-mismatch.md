# Mobius 콜백 — 문서와 코드 불일치 정리

- 작성: 2026-11-04
- 기준 코드: `lgk-refactor` 브랜치 `1c9a181`
- 기준 문서: 「Mobius CNT · CIN 구조 정리」(2026-10-02)
- 확인 방법: 코드와 문서 대조. 실제 실행으로 재현하지는 않았다.

## 요약

| # | 항목 | 문제 파일 | 문서 위치 | 고칠 쪽 |
|---|---|---|---|---|
| 1 | 이른 보고로 단계 건너뜀 | `callbacks/conveyor.py` | 3장 공통 가드, 2-2 | 코드 |
| 2 | 늦게 온 `failed`로 fault 발생 | `callbacks/conveyor.py` | 3장 공통 가드 | 코드 |
| 3 | 오배송 판정 누락 | `callbacks/agv.py` | 3장 공통 가드, 3-4 | 코드 |
| 4 | fault 중 일부 AGV 보고 처리 | `callbacks/agv.py` | 3장 공통 가드, 3-4 | 문서 + 코드 일부 (결정 필요) |

## 1. 이른 보고로 단계 건너뜀

**문제 코드:** `callbacks/conveyor.py:71-73`

```python
if is_at_or_past(stage, "keycap_reached"):
    return                                   # 재전송
set_stage(order_id, "keycap_reached")
```

| 구분 | 내용 |
|---|---|
| 문서 | 단계가 맞지 않는 보고는 재전송으로 보고 버린다 (3장 공통 가드). `keycap_reached`는 `keycap_dispensed` 다음 단계다 (2-2). |
| 코드 | 이미 지난 보고만 거른다. 아직 도달하지 않은 단계의 보고는 거르지 않고, `set_stage`(`services/process.py:49`)에도 전이 검사가 없다. |

**발생 상황**

1. 주문이 `assigned`이고 키캡 배출이 끝나지 않았다.
2. `cnt_keycap_conveyor`에 4개 모두 `done`이 들어온다.
3. 주문이 `keycap_reached`로 바로 넘어간다.
4. 이후 도착하는 `cnt_keycap_dispense`는 `KEYCAP_STAGES` 가드(`callbacks/warehouse.py:10`, `:61`)에 걸려 버려진다.

결과적으로 키캡이 주문과 일치하는지 확인하지 않은 채 다음 공정으로 넘어간다.

**같은 패턴:** `on_tray_conveyor`(`callbacks/conveyor.py:93`)도 같다. 이른 `section2: done`이 오면 `pickup_reached`로 넘어가고 창고까지 해제된다(`:101-104`).

**수정 방향**

- 창고 콜백처럼 허용 단계를 명시한다.

  | 보고 | 허용 단계 |
  |---|---|
  | `keycap_conveyor` | `keycap_dispensed` |
  | `tray_conveyor` `section1` | `keycap_packed` |
  | `tray_conveyor` `section2` | `board_packed` |

- 허용 단계가 아니면 경고 로그만 남기고 버린다.

**주의:** 마지막 `cnt_keycap_dispense`보다 컨베이어 보고가 먼저 도착하면 그 보고가 버려져 `keycap_dispensed` 단계에서 `stage_timeout`까지 멈춘다. 실제 장비에서 두 보고의 간격을 확인해야 한다.

## 2. 늦게 온 `failed`로 fault 발생

**문제 코드:** `callbacks/conveyor.py:59-66`

```python
failed = [f for f in KEYCAP_FIELDS if result.get(f) == "failed"]
if failed:
    ...
    set_fault(order_id, f"keycap_{section}_belt_jammed", ...)
    return
```

| 구분 | 내용 |
|---|---|
| 문서 | 단계가 맞지 않는 보고는 재전송으로 보고 버린다 (3장 공통 가드). |
| 코드 | 단계 확인 없이 `failed`부터 처리한다. `_order()`(`:34-48`)도 주문 일치와 fault 여부만 확인한다. |

**발생 상황**

1. 주문이 `tray_reached`까지 정상 진행했다.
2. 이전 `cnt_keycap_conveyor`의 `failed` 보고가 늦게 또는 다시 도착한다.
3. 같은 주문이 아직 창고에 있으므로 `_order()`를 통과한다. `warehouse:current_order`는 `pickup_reached`에서 풀린다.
4. `set_fault`가 fault를 세우고 워치독 마감을 해제한다(`services/process.py:81`).

결과적으로 정상 주문이 관리자가 해제할 때까지 멈춘다.

**같은 패턴:** `on_tray_conveyor`(`callbacks/conveyor.py:89-92`)의 `failed`도 단계를 확인하지 않는다.

**수정 방향:** 1번에서 만드는 허용 단계 검사를 `failed` 처리보다 앞에 둔다. 두 항목을 함께 고치면 된다.

## 3. 오배송 판정 누락

**문제 코드:** `callbacks/agv.py:48-50`, `:163-168`

```python
station_id = str(con.get("station_id") or "")
if station_id:
    return r.get(f"{STATION_ORDER_PREFIX}{station_id}")
```

| 구분 | 내용 |
|---|---|
| 문서 | `arrived`가 배정과 다른 조립대에서 오면 `drop_failed` (3-4). 대상 주문은 `order_id → station_id → 조립대 훑기` 순으로 찾는다 (3장 공통 가드). |
| 코드 | `order_id`가 있으면 문서대로 동작한다. 없으면 보고된 조립대의 주문을 대상으로 잡으므로 `con_sid`와 `assigned_sid`가 항상 같고, 오배송 판정이 실행되지 않는다. |

**발생 상황:** 조립대 1에 배정된 주문을 실은 AGV가 조립대 2에서 `{"station_id": "2", "status": "arrived"}`를 보낸다.

| 조립대 2 상태 | 코드 동작 | 판정 |
|---|---|---|
| 비어 있음 | 대상 주문 없음 경고 (`:123-125`) | `drop_failed` 없음 |
| `arrived` 이후 주문 있음 | 재전송으로 무시 (`:147-149`) | `drop_failed` 없음 |
| `loaded` 이전 주문 있음 | 단계 불일치로 무시 (`:150-152`) | `drop_failed` 없음 |

실제 주문은 `loaded`에 남아 120초 뒤 `stage_timeout`이 난다. 관리자에게는 오배송이 아니라 시간 초과로 보인다.

**관련 불일치:** `station_id`가 있으면 그 조립대에서 주문을 못 찾아도 조립대 훑기로 넘어가지 않는다. 문서의 탐색 순서와 다르다.

**수정 방향**

- `order_id` 없는 `arrived`는 `station_id` 조회를 건너뛰고 조립대를 훑어 `loaded` 주문을 찾는다. 기존 로직(`:63-70`)을 재사용할 수 있다.
- 그러면 `:165`의 비교가 실제 배정 조립대와 이루어져 `drop_failed`가 동작한다.
- AGV가 항상 `order_id`를 보낸다면 지금 당장 발생하지는 않는다. AGV 담당자에게 확인이 필요하다.

## 4. fault 중 일부 AGV 보고 처리

**문제 코드:** `callbacks/agv.py:96-102`, `:108-120`, `:127-130`. fault 가드는 그 뒤인 `:140-143`에 있다.

| 구분 | 내용 |
|---|---|
| 문서 | fault 걸린 주문의 AGV 보고는 버린다 (3장 공통 가드). |
| 코드 | `TRANSITIONS`에 해당하는 보고만 버린다. 아래 세 종류는 fault 중에도 처리한다. |

| 보고 | 코드 위치 | 동작 | 판단 |
|---|---|---|---|
| `parked` | `:96-102` | AGV를 `idle`로 변경 | 유지. 문서 3-4도 "주문과 무관"으로 설명한다. |
| `discarded` | `:108-120` | 조립대 해제 | 유지. 버려도 300초 뒤 워치독이 강제 해제하므로 조립대만 늦게 풀린다. |
| `load_failed`, `unload_failed`, `agv_broken` | `:127-130` | fault 설정 | 받는 것은 유지(코드 주석에 의도 명시). 덮어쓰기는 아래 참고. |

**실제 문제:** fault 칸은 주문당 하나이고 `set_fault`(`services/process.py:75-79`)가 기존 값을 덮어쓴다. 예를 들어 `drop_failed` 중 `agv_broken`이 오면 처음 원인인 `drop_failed`가 주문 정보에서 사라지고, 로그에만 남는다. 또 `:127-130`의 fault 보고는 단계도 확인하지 않는다.

**수정 방향**

1. 문서: 3장 공통 가드에 `parked`, `discarded`, AGV fault 보고를 예외로 명시한다.
2. 코드: 이미 fault가 있을 때 처리 방식을 정한다.

| 방식 | 장점 | 단점 |
|---|---|---|
| 첫 fault 유지 (권장), 이후 보고는 로그만 | 처음 원인이 보존된다 | 관리자 화면에 최신 고장이 보이지 않는다 |
| 최신 fault로 교체 (현행) | 현재 상태가 바로 보인다 | 처음 원인이 주문 정보에서 사라진다 |

## 확인·결정 필요

- [ ] 마지막 키캡 배출 보고와 컨베이어 보고의 도착 순서와 간격 (1번)
- [ ] AGV가 `arrived`에 `order_id`를 항상 싣는지 (3번)
- [ ] 이미 fault가 있을 때 첫 fault와 최신 fault 중 무엇을 남길지 (4번)
