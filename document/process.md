# 작업 내역

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
