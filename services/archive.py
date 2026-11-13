"""주문 종료 공통 경로 — 아카이브 · 집계 · 자원 해제.

종료 지점이 다섯이다. 전부 여기로 모은다.
    완료 버튼           routes/station.py  → station_service._complete_station
    노쇼(프론트)        routes/station.py  /unclaim
    노쇼(워치독 백업)    services/watchdog.py  arrived 마감
    타임아웃            services/watchdog.py  그 외 stage 마감
    관리자 단건 중단     routes/admin.py  /admin/order/<id>/abort → abort_order (reason=admin_abort)

아카이브에는 '아예 끝난 주문'만 들어가므로 기록의 final_status 는 completed 하나다.
정상 완료가 아닌 종료는 reason 으로 구분한다 — 노쇼는 unclaimed, 마감 초과는 timeout.
Redis·cnt_process 의 stage 는 그대로 unclaimed / completed 를 쓴다 (status 16값).

저장 (Redis ERD 5장)
    orders:archive:{YYYYMMDD}   hash   field=order_id  value=기록 JSON   TTL 90일
"""
import json
import time
from datetime import datetime

from config import (AGV_KEY,
                    ARCHIVE_PREFIX,
                    ARCHIVE_TTL,
                    DEADLINE_KEY,
                    DISCARD_TIMEOUT_SEC,
                    ORDER_DONE_TTL,
                    QUEUE_KEY,
                    STATION_ORDER_PREFIX,
                    STATION_VERIFIED_PREFIX,
                    STATION_CALL_PREFIX,
                    WAREHOUSE_ORDER_KEY,
                    KST,
                   )
from infra.extensions import r
from infra.keys import _touch_order
from infra.logger import logger


def finish_order(order_id, final_stage, reason=None, assign=True):
    """주문을 끝낸다. stage 전이 → 아카이브 → 자원 해제 순서.

    stage 를 먼저 넘기는 이유 — set_stage 가 워치독을 풀고 cnt_process 를 밀어서,
    아카이브를 쓰는 동안 이 주문에 또 마감이 걸리는 일이 없다.
    """
    key = f"order:{order_id}"
    od = r.hgetall(key)
    if not od:
        logger.warning(f"[finish] {order_id} 주문 없음 — 건너뜀")
        return None

    from services.process import set_stage, TERMINAL          # 순환 import 회피

    if od.get("stage") in TERMINAL:
        # 끝난 주문을 다시 끝내지 않는다. 특히 unclaimed 는 AGV 폐기 보고까지 조립대를 잡고 있는데
        # (_release_resources), 여기로 completed 가 들어오면 그 조립대가 폐기 전에 풀린다.
        # 조립대 완료 버튼·조립 타임아웃이 그 경로다 (routes/station.py, station_service._complete_station).
        logger.warning(f"[finish] {order_id} 이미 {od.get('stage')} — {final_stage} 요청 무시")
        return None

    if reason:
        r.hset(key, "reason", reason)
    set_stage(order_id, final_stage)                # cnt_process push + 워치독 해제
    _archive(order_id, od, reason)
    _release_resources(order_id, od, final_stage, assign=assign)

    if final_stage == "unclaimed":
        # 폐기 마감. set_stage 뒤여야 한다 — unclaimed 는 TERMINAL 이라
        # set_stage 안의 watchdog.disarm 이 먼저 돌면서 예약을 지운다.
        r.zadd(DEADLINE_KEY, {order_id: time.time() + DISCARD_TIMEOUT_SEC})
        logger.info(f"[finish] {order_id} 폐기 대기 — 조립대 {od.get('station_id')} 는 "
                    f"AGV discarded 까지 잡아 둔다 (마감 {DISCARD_TIMEOUT_SEC}s)")
    else:
        # 끝난 주문 해시는 오래 둘 이유가 없다. 기록은 아카이브에 있다.
        _touch_order(order_id, ORDER_DONE_TTL)

    logger.info(f"[finish] {order_id} {final_stage}"
                + (f" (reason={reason}, stage={od.get('stage')})" if reason else ""))
    return order_id


def _archive(order_id, od, reason):
    date = datetime.now(KST).strftime("%Y%m%d")
    akey = f"{ARCHIVE_PREFIX}{date}"
    if r.hexists(akey, order_id):
        return                                       # 두 경로가 겹쳐도 한 번만

    colors = [c for c in (od.get("colors") or "").split(",") if c]
    rec = {
        "order_id": order_id,
        "order_seq": int(od.get("order_seq") or 0),
        "station_id": od.get("station_id"),
        "board": od.get("board"),
        "keycap": od.get("keycap"),
        "colors": colors,
        "final_status": "completed",         # 아카이브는 '끝난 주문'만 들어온다
        "created_at": od.get("created_at"),
        "ended_at": datetime.now(KST).isoformat(timespec="seconds"),
    }
    if reason:
        rec["reason"] = reason
        rec["ended_at_stage"] = od.get("stage")      # 어느 단계에서 끊겼는지
    if od.get("fault"):
        rec["fault"] = od["fault"]
        rec["fault_section"] = od.get("fault_section")      # 어느 부품·선반에서 멈췄나
    if od.get("pickup_wait_extends"):
        rec["pickup_wait_extends"] = int(od["pickup_wait_extends"])
    if od.get("call_count"):
        rec["call_count"] = int(od["call_count"])       # 관리자를 몇 번 불렀나

    r.hset(akey, order_id, json.dumps(rec, ensure_ascii=False))
    r.expire(akey, ARCHIVE_TTL)

    # stats:* 집계는 폐기했다 (2026-09-18). 쓰는 화면이 없어서 종료 경로마다
    # INCR 4건을 더 치를 이유가 없다. 필요해지면 아카이브 해시를 날짜별로 세면 된다 —
    # 같은 값이 orders:archive:{date} 의 레코드에 전부 들어 있다.


def _release_resources(order_id, od, final_stage=None, assign=True):
    """창고 먼저, 조립대 나중. 각각 다음 배정을 시도한다.

    타임아웃으로 끝난 주문은 창고 구간 한복판일 수 있다. 창고를 안 풀면
    그 뒤 주문이 통째로 멈춘다 — 완료 버튼 경로에는 없던 상황이다.

    노쇼(unclaimed)만 예외다. 조립대를 여기서 풀면 station:current_order 가 지워지고,
    cnt_process 는 조립대에 올라간 주문만 싣기 때문에(process.active_order_ids)
    **AGV 가 트레이를 아직 들고 있는데 폐기 명령이 스냅샷에서 사라진다.**
    실측 0.53초 만에 사라졌다. 그래서 조립대는 AGV 가 discarded 를 올릴 때까지 잡아 둔다
    (callbacks/agv.py). 안 오면 워치독이 DISCARD_TIMEOUT_SEC 뒤에 강제로 푼다.
    """
    from services.dispatch import release_station, release_warehouse

    if r.get(WAREHOUSE_ORDER_KEY) == order_id:
        release_warehouse(assign=assign)

    if final_stage == "unclaimed":
        return                                   # 조립대는 폐기 완료까지 유지

    sid = od.get("station_id")
    if sid and r.get(f"{STATION_ORDER_PREFIX}{sid}") == order_id:
        r.delete(f"{STATION_VERIFIED_PREFIX}{sid}")
        r.delete(f"{STATION_CALL_PREFIX}{sid}")      # 남기면 다음 사용자가 부른 것처럼 보인다
        release_station(sid, assign=assign)


def release_station_after_discard(order_id, assign=True):
    """AGV 가 폐기를 마쳤다 → 잡아 두던 조립대를 푼다.

    callbacks/agv.py 의 discarded 분기와 워치독의 폐기 마감, 두 곳에서 부른다.
    어느 쪽이 먼저 와도 되도록 조립대에 그 주문이 남아 있을 때만 움직인다.

    **stage 가 unclaimed 인 주문만 푼다.** AGV 가 엉뚱한 order_id 를 실었거나 station_id 의
    조립대에 살아 있는 주문이 있으면, 그 주문의 조립대가 풀려 cnt_process 에서 사라지고
    조립대가 이중 배정된다. 마감 해제(disarm)도 이 검사 뒤에 둔다 — 앞에 두면 남의 주문 시계가 꺼진다.
    """
    from services.dispatch import release_station
    from services import watchdog

    od = r.hgetall(f"order:{order_id}")
    if not od:
        return None                              # TTL 로 이미 사라졌다
    if od.get("stage") != "unclaimed":
        logger.warning(f"[finish] {order_id} stage={od.get('stage')} — unclaimed 가 아니라 폐기 해제 무시")
        return None
    watchdog.disarm(order_id)
    sid = od.get("station_id")
    if not sid or r.get(f"{STATION_ORDER_PREFIX}{sid}") != order_id:
        return None                              # 이미 풀렸다
    r.delete(f"{STATION_VERIFIED_PREFIX}{sid}")
    r.delete(f"{STATION_CALL_PREFIX}{sid}")
    _touch_order(order_id, ORDER_DONE_TTL)
    logger.info(f"[finish] {order_id} 폐기 완료 — 조립대 {sid} 해제")
    return release_station(sid, assign=assign)   # assign 이면 try_assign_next 까지 돈다

# ── 관리자 단건 중단 ─────────────────────────────────────────────────────
ABORT_REASON = "admin_abort"

# AGV 가 트레이를 들고 있어 폐기(unclaimed) 경로로 보낼 수 있는 구간.
# 그냥 completed 로 지우면 노쇼 때와 같은 일이 난다 — 주문이 cnt_process 에서 사라져
# AGV 는 트레이를 든 채 받을 명령이 없다 (_release_resources 주석).
ABORT_DISCARD_STAGES = ("loaded", "arrived")

# collector 가 주문마다 까는 목록 (warehouse·conveyor·robot_arm 콜백)
_COLLECTOR_SLOTS = ("keycap", "board", "keycap_pack")

_ABORT_ACTION = {
    "queued": "대기열에서 뺐다. 할 일 없음",
    "warehouse": "벨트·트레이 위 부품을 사람이 치웠는지 확인",
    "pickup_reached": "픽업대 트레이를 사람이 회수",
    "discard": "AGV 가 폐기장소로 가는지 확인. discarded 가 오면 조립대가 풀린다 (안 오면 300초 뒤 워치독이 강제 해제)",
    "removed": "트레이를 사람이 회수했다고 보고 끝냈다",
    "station": "조립대의 트레이·부품을 사람이 회수",
    "force_release": "폐기 보고를 기다리지 않고 조립대를 풀었다. AGV 가 트레이를 정말 비웠는지 확인",
}


def _clear_scratch(order_id):
    """끝난 주문이 collector 를 물고 있으면 다음 주문 판정을 오염시킨다 (불변식 6)."""
    from services import collector
    for slot in _COLLECTOR_SLOTS:
        collector.clear(order_id, slot)
    collector.reset_gate(order_id)


def _assign_after_abort():
    """중단 뒤 배정 — AGV 가 대기장소에 있을(idle) 때만 바로 부른다. (배정된 주문, 안내 문구)

    AGV 가 busy 면 부르지 않는다. 언젠가 parked 가 오고 그 콜백이 try_assign_next 를 부른다
    (callbacks/agv.py). AGV 가 idle 이면 그 트리거가 다시 오지 않는다 — 대기열이 꽉 차
    POST /order 도 409 로 빠지면 대기 주문이 영영 배정되지 않았다 (가상 테스트 tests/sim.py 에서 확인).

    idle 은 agv:occupancy 다. parked 에서 idle, loaded 에서 busy 가 되므로 대기장소 → 픽업대
    이동 중에도 idle 로 읽힌다 (그 사이 보고가 없다). 창고·픽업대·조립대·AGV 고장 게이트는
    try_assign_next 가 그대로 본다 — AGV 고장 보고가 남아 있으면 idle 이어도 배정되지 않는다.
    """
    from services.dispatch import try_assign_next

    if r.get(AGV_KEY) != "idle":
        return None, "AGV 가 복귀(parked)하면 다음 주문이 배정된다"
    assigned = try_assign_next()
    if assigned:
        return assigned, f"AGV 대기 중이라 다음 주문 {assigned} 를 바로 배정했다"
    return None, "AGV 는 대기 중이지만 배정 조건이 안 맞아 대기 (재고·픽업대·AGV 고장·빈 조립대)"


def abort_order(order_id, discard=True):
    """관리자가 주문 하나만 끝낸다. fault 해제(그 자리에서 이어가기)와 라인 전체 리셋 사이의 길.

    트레이가 지금 어디 있느냐로 갈린다.

      queued                    대기열에서 빼고 아카이브만. 잡은 자원이 없다
      assigned ~ board_packed   **벨트 위 트레이·부품을 사람이 먼저 치운 뒤에 부른다.**
                                창고를 풀고, AGV 가 대기 중이면 다음 주문이 바로 배정된다
      pickup_reached            픽업대 트레이를 사람이 회수 (워치독 pickup_reached 마감과 같은 처리)
      loaded · arrived          discard=True(기본)  → unclaimed 로 끝낸다. AGV 가 폐기하고
                                                     discarded 를 올리면 조립대가 풀린다 (노쇼와 같은 길)
                                discard=False       → 트레이를 사람이 회수했다. completed 로 끝낸다
      verified · received       손님 앞이다. completed 로 끝낸다
      unclaimed                 이미 끝났고 폐기 보고만 기다리는 중 → 조립대를 강제로 푼다
                                (워치독 폐기 마감 300초를 기다리지 않는 것)
      completed                 이미 끝났다 → ValueError

    아카이브는 reason=admin_abort 로 남는다. 대시보드는 reason 없는 주문만 완료로 세므로
    통계에 섞이지 않는다. 걸려 있던 fault 는 아카이브에 남기고 주문 해시에서는 지운다 —
    남겨 두면 머신이 cnt_process 에서 fault 를 보고 폐기 명령에도 멈춰 선다.

    재고 칸 status·선반 차단(warehouse:driver_failed)은 건드리지 않는다. 그건 주문이 아니라
    장비 상태다 — 장비를 고쳐 정상 status 가 올라오면 콜백이 푼다 (callbacks/warehouse.py).
    고쳤다는 걸 사람이 선언하는 길은 fault/clear 다.

    배정은 AGV 상태로 갈린다 (_assign_after_abort). AGV 가 대기장소(idle)면 바로 배정을 시도하고,
    busy 면 부르지 않는다 — AGV 가 복귀하며 올리는 parked 에서 배정된다.
    assigned_next 는 이 중단으로 새로 배정된 주문 (없으면 빈 목록).

    없는 주문은 LookupError, 이미 끝난 주문은 ValueError.
    """
    from services.process import push_process

    key = f"order:{order_id}"
    od = r.hgetall(key)
    if not od:
        raise LookupError(order_id)
    stage = od.get("stage")
    sid = od.get("station_id")
    fault = od.get("fault") or None

    if stage == "completed":
        raise ValueError("이미 끝난 주문입니다")

    result = {"order_id": order_id, "from_stage": stage, "fault": fault, "station_id": sid}
    waiting = set(r.lrange(QUEUE_KEY, 0, -1)) - {order_id}

    def _newly_assigned():
        return sorted(waiting - set(r.lrange(QUEUE_KEY, 0, -1)))

    if stage == "unclaimed":
        held = bool(sid) and r.get(f"{STATION_ORDER_PREFIX}{sid}") == order_id
        if not held:
            raise ValueError("이미 끝났고 조립대도 풀린 주문입니다")
        r.hdel(key, "fault", "fault_section")
        release_station_after_discard(order_id, assign=False)
        _clear_scratch(order_id)
        # 폐기 보고가 유실된 경우라 AGV 는 이미 parked 일 가능성이 높다 — 그러면 남은 트리거가 없다
        _, note = _assign_after_abort()
        push_process()
        logger.warning(f"[abort] {order_id} unclaimed — 폐기 보고 없이 조립대 {sid} 강제 해제 · {note}")
        return {**result, "final_stage": "unclaimed", "assigned_next": _newly_assigned(),
                "action": f"{_ABORT_ACTION['force_release']} — {note}"}

    from services.recovery import WAREHOUSE_STAGES

    if stage in ABORT_DISCARD_STAGES and discard:
        final, action = "unclaimed", "discard"
    elif stage in ABORT_DISCARD_STAGES:
        final, action = "completed", "removed"
    elif stage == "queued":
        final, action = "completed", "queued"
    elif stage in WAREHOUSE_STAGES:
        final, action = "completed", "warehouse"
    elif stage == "pickup_reached":
        final, action = "completed", "pickup_reached"
    else:
        final, action = "completed", "station"

    r.lrem(QUEUE_KEY, 0, order_id)           # queued 가 아니면 아무 일도 없다
    # 자원 해제에서는 배정하지 않는다 — fault·collector 를 치운 뒤 AGV 상태를 보고 아래에서 부른다
    finish_order(order_id, final, reason=ABORT_REASON, assign=False)

    # finish_order 가 fault 를 아카이브에 옮겨 적은 뒤에 지운다
    r.hdel(key, "fault", "fault_section")
    _clear_scratch(order_id)

    _, note = _assign_after_abort()

    # 자원 해제 뒤 배정이 안 되면 아무도 push 하지 않아서 스냅샷에 이 주문이 completed 로 남는다
    # (dispatch.release_station 주석). 중단은 명시적으로 지운다. 내용이 같으면 push 는 안 나간다.
    push_process()

    logger.warning(f"[abort] {order_id} {stage} -> {final} (fault={fault}, discard={discard}) · {note}")
    return {**result, "final_stage": final, "assigned_next": _newly_assigned(),
            "action": f"{_ABORT_ACTION[action]} — {note}"}
