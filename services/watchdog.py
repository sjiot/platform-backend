import time

from infra import latency
from infra.extensions import r
from infra.logger import logger

from config import (DEADLINE_KEY, DISCARD_TIMEOUT_SEC, STAGE_BUSY,
                    STATION_ORDER_PREFIX, STATION_CALL_PREFIX)

# 명세 7장. 사람·이동이 끼는 셋은 기계 속도로 재면 안 된다.
STAGE_TIMEOUT = {
    "assigned": 120,
    "keycap_mismatched": 120,
    "keycap_dispensed": 120,
    "keycap_reached": 120,
    "keycap_packed": 120,
    "tray_reached": 120, # AGV 폐기 후 위치 이동 문제. 직접 확인 후 유동적으로 처리하도록 변경 필요
    "board_mismatched": 120,
    "board_packed": 120,
    "pickup_reached": 300,  # AGV 픽업. 앞 주문이 노쇼(3분)면 그만큼 AGV가 묶이므로 그 위로 잡는다
    "loaded": 120,      # AGV 이동 거리에 좌우됨
    "arrived": 300,     # 프론트가 3분에 /unclaim을 친다. 이건 그게 실패했을 때의 백업
    "verified": 120,
    # received(조립 중)는 마감이 없다. 사용자가 완료를 누르거나 관리자가 중단할 때까지 조립대를 잡는다
}

AGV_EXTEND_SEC = 10

CALL_EXTEND = {
    "arrived": 180,     # 부품이 제대로 안 왔다 → 노쇼 백업(300s)이 먼저 터지지 않게
    "verified": 180,    # 로봇팔이 트레이를 안 내려준다 (기본 60s 로는 조치할 시간이 없다)
}

MAX_CALL_EXTENDS = 2

def _call_bonus(order_id, stage):
    """이 주문에 호출이 열려 있으면 stage 별 연장 초, 아니면 0.

    arm() 이 이 값을 태워야 한다. set_stage 가 전이마다 시계를 다시 거니까,
    호출 시점에 한 번만 밀어두면 arrived → verified 로 넘어가는 순간 연장이 날아간다.
    (관리자가 오는 도중에 QR 인증이 끝나는 경우가 정확히 그 경로다.)
    """
    sec = CALL_EXTEND.get(stage)
    if not sec:
        return 0
    sid = r.hget(f"order:{order_id}", "station_id")
    if not sid:
        return 0
    if r.hget(f"{STATION_CALL_PREFIX}{sid}", "order_id") != order_id:
        return 0
    if int(r.hget(f"order:{order_id}", "call_count") or 0) > MAX_CALL_EXTENDS:
        return 0
    return sec


def arm(order_id, stage):
    """set_stage에서 부른다. 이전 예약을 지우고 새로 건다."""
    ttl = STAGE_TIMEOUT.get(stage)
    if not ttl:
        r.zrem(DEADLINE_KEY, order_id)
        return
    bonus = _call_bonus(order_id, stage)
    r.zadd(DEADLINE_KEY, {order_id: time.time() + ttl + bonus})
    if bonus:
        logger.info(f"[watchdog] {order_id} {stage} 마감 {ttl}+{bonus}s — 관리자 호출 진행 중")


def timeout_section(stage):
    """마감을 넘긴 stage 에서 '움직여야 했던 설비'. fault_section 으로 나간다.

    표를 새로 만들지 않고 config.STAGE_BUSY 를 그대로 쓴다 — 두 곳에 두면 갈라진다.
    assigned 만 항목이 2개라(빈 트레이 투입 · 키캡 배출) 마지막 하나를 쓰고,
    둘 중 어디서 끊겼는지는 _expire 의 collector 로그가 남긴다.
    """
    machines = STAGE_BUSY.get(stage) or []
    return machines[-1] if machines else None


def disarm(order_id):
    r.zrem(DEADLINE_KEY, order_id)


def sweep():
    """스케줄러가 1초 간격으로 부른다. 만료된 것만 뽑아 처리."""
    for order_id in r.zrangebyscore(DEADLINE_KEY, 0, time.time()):
        if r.zrem(DEADLINE_KEY, order_id) == 0:
            continue                      # 다른 워커가 먼저 가져갔다
        started = time.perf_counter()
        token = latency.begin_trace()
        ok = True
        try:
            _expire(order_id)
        except Exception:
            ok = False
            logger.exception(f"[watchdog] {order_id} 처리 실패")
        finally:
            latency.record_request("watchdog_expire", (time.perf_counter() - started) * 1000, ok=ok, order_id=order_id)
            latency.end_trace(token)


def _expire(order_id):
    # 순환 import 회피 — process가 watchdog.arm을 쓰므로 여기선 함수 안에서 부른다
    from services.archive import finish_order
    from services.process import set_fault
    from services import collector

    stage = r.hget(f"order:{order_id}", "stage")
    if stage is None:
        return                                        # TTL로 이미 사라진 주문
    if stage == "received":
        # 조립 상한은 없앴다 (사용자 완료 · 관리자 중단으로만 끝난다). 배포 전에 걸린 예약만 여기로 온다
        logger.info(f"[watchdog] {order_id} received 마감 예약 무시 — 조립 상한 없음")
        return

    if stage in ("assigned", "keycap_mismatched"):
        logger.error(f"[watchdog] {order_id} {stage} 마감 — "
                     f"미배출 {collector.remaining(order_id, 'keycap')} "
                     f"오배출 {collector.wrong(order_id, 'keycap')}")
    else:
        logger.error(f"[watchdog] {order_id} {stage} 마감")

    if stage == "unclaimed":
        from services.archive import release_station_after_discard
        logger.error(f"[watchdog] {order_id} 폐기 미보고 {DISCARD_TIMEOUT_SEC}s — "
                     f"조립대 강제 해제 (AGV 확인 필요)")
        release_station_after_discard(order_id)
    elif stage == "arrived":
        finish_order(order_id, "unclaimed", reason="unclaimed")   # 노쇼 백업 경로
    elif stage == "pickup_reached":
        # 연장을 다 쓰고 내려온 경우에만 여기 온다. AGV가 돌아올 가망이 없다고 본 것이므로
        # 종료는 하되 'AGV 때문에 끝났다'는 사실은 아카이브에 남긴다.
        r.hset(f"order:{order_id}", mapping={"fault": "stage_timeout",
                                            "fault_section": "agv"})
        finish_order(order_id, "completed", reason="timeout")
    else:
        set_fault(order_id, "stage_timeout", section=timeout_section(stage))

def _pickup_waiters():
    """픽업대에서 AGV를 기다리는 주문. 조립대에 배정된 주문만 대상이다."""
    out = []
    for sid in ("1", "2", "3"):
        oid = r.get(f"{STATION_ORDER_PREFIX}{sid}")
        if oid and r.hget(f"order:{oid}", "stage") == "pickup_reached":
            out.append(oid)
    return out


def extend_pickup_waiters(sec=AGV_EXTEND_SEC):
    """cnt_agv 보고 1건마다 픽업대 대기 주문의 마감을 밀어준다 (callbacks/agv.py 에서 호출).

    마감 시점에 AGV 상태를 보고 판단하지 않는다 — 그러면 AGV가 막 풀린 찰나에 걸린
    주문이 억울하게 종료되고(실측 사례), 반대로 AGV가 그 상태로 죽으면 영원히 밀린다.
    '보고가 왔을 때만 1회' 로 두면 둘 다 생기지 않는다.
    """
    for oid in _pickup_waiters():
        dl = r.zscore(DEADLINE_KEY, oid)
        if dl is None:
            continue
        n = r.hincrby(f"order:{oid}", "pickup_wait_extends", 1)
        r.zadd(DEADLINE_KEY, {oid: dl + sec})
        logger.info(f"[watchdog] {oid} pickup_reached 마감 +{sec}s ({n}회) — AGV 보고 수신")


def rearm_pickup_waiters():
    """AGV가 대기장소로 복귀(parked)했다 → 픽업대 대기 주문의 시계를 새로 건다.

    마감은 트레이가 픽업대에 닿은 시각부터 재는데, 그때 AGV가 앞 주문에 묶여 있었다면
    그건 이 주문 잘못이 아니다. AGV가 자유로워진 시점을 기준선으로 다시 잡는다.
    여기서부터 마감 안에 안 실으면 그건 AGV 문제가 맞다.
    """
    for oid in _pickup_waiters():
        r.hdel(f"order:{oid}", "pickup_wait_extends")
        arm(oid, "pickup_reached")
        logger.info(f"[watchdog] {oid} pickup_reached 마감 재장전 — AGV 대기장소 복귀")

def extend_for_call(order_id, stage):
    """호출 접수 시 현재 마감을 그만큼 밀어준다. 실제로 민 초를 반환한다.

    현재 시각 기준 재장전이 아니라 **기존 마감에 덧붙인다** (extend_pickup_waiters 와 같은 방식).
    재장전으로 하면 마감 직전에 계속 눌러 같은 stage 에 영원히 머무를 수 있다.
    호출 상태·call_count 가 먼저 기록된 뒤에 부를 것 — _call_bonus 가 그것들을 본다.
    """
    sec = _call_bonus(order_id, stage)
    if not sec:
        return 0
    dl = r.zscore(DEADLINE_KEY, order_id)
    if dl is None:
        return 0                      # 마감이 없는 stage(종료·TERMINAL)면 밀 것도 없다
    r.zadd(DEADLINE_KEY, {order_id: dl + sec})
    logger.info(f"[watchdog] {order_id} {stage} 마감 +{sec}s — 관리자 호출")
    return sec

SWEEP_LOCK = "watchdog:lock"
SWEEP_INTERVAL = 1.0


def start_sweeper(interval=SWEEP_INTERVAL):
    """백그라운드 스레드로 sweep을 돌린다. APScheduler 의존성 없이.

    gunicorn 워커가 여러 개면 프로세스마다 스레드가 뜨므로 Redis 락으로 한 번만 돌게 한다.
    락이 없으면 같은 주문에 fault를 워커 수만큼 건다.
    """
    import threading

    def loop():
        while True:
            try:
                # 락을 잡은 프로세스만 이번 턴을 처리한다. TTL이 지나면 아무나 다시 잡는다.
                if r.set(SWEEP_LOCK, "1", nx=True, ex=max(2, int(interval * 2))):
                    sweep()
            except Exception:
                logger.exception("[watchdog] sweep 루프 오류")
            time.sleep(interval)

    t = threading.Thread(target=loop, name="watchdog-sweeper", daemon=True)
    t.start()
    logger.info(f"[watchdog] sweeper 시작 (interval={interval}s)")
    return t
