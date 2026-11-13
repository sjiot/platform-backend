"""큐 → 조립대 배정.

배정 게이트 넷.
    1. 창고가 비었나        warehouse:occupancy == idle   (주문 1건씩 처리)
    2. 픽업대가 비었나      pickup_reached 주문이 없다     (트레이를 AGV가 실어 가야 다음 주문)
    3. 조립대 빈 칸이 있나   station:current_order:{1,2,3}
    4. 이 주문이 쓰는 카트리지 5칸이 멀쩡한가   stock.blockers()
그 밖에 주문 없는 AGV 고장(agv_fault)이 걸려 있으면 배정하지 않는다.

픽업대 게이트는 창고가 AGV보다 최대 한 주문만 앞서가게 한다. 창고는 pickup_reached 에서
풀리지만, 그 트레이를 AGV가 싣는(loaded) 순간까지 다음 주문은 창고로 넘어가지 않는다 —
    주문1 pickup_reached                → 창고 idle, 픽업대가 차 있어 주문2 대기
    주문1 loaded                        → 주문2 배정 (AGV 배송과 창고 작업이 겹친다)
    주문2 pickup_reached, AGV 배송 중   → 대기. AGV가 parked 뒤 와서 싣으면(loaded) 주문3 배정
그래서 픽업대에 트레이가 두 개 쌓이지 않는다. 예전에는 'AGV idle(parked)' 게이트로
막았는데, 그건 AGV가 창고보다 빨리 픽업대에 온다는 가정에 기댔다.
픽업대 상태는 따로 키를 두지 않고 stage 로 읽는다 — 리셋·복구 때 어긋날 키가 하나 줄어든다.

카트리지 idle/busy는 게이트로 쓰지 않는다. 창고가 idle이면 카트리지도 idle이어야 하고,
idle CIN 하나만 유실돼도(주문당 카트리지 콜백이 10건) 그 칸이 영구히 busy로 남아 교착된다.

배정 트리거 — 배정을 막던 조건이 풀리는 지점 전부에서 try_assign_next()를 부른다.
막혀 있으면 즉시 None으로 빠지므로 값이 싸다.
    POST /order 접수 직후          큐에 새 주문                    routes/order.py
    pickup_reached                 창고가 비었음 (벨트 비었음)      callbacks/conveyor.py
    loaded                         픽업대가 비었음                  callbacks/agv.py
    parked                         (무해한 재시도)                  callbacks/agv.py
    completed / unclaimed          조립대가 비었음                  routes/station.py · watchdog
    카트리지 정상 복귀              막혀 있던 주문이 풀림            callbacks/warehouse.py
"""
import json

from config import (QUEUE_KEY, WAREHOUSE_KEY, STATION_KEY,
                    WAREHOUSE_ORDER_KEY, STATION_ORDER_PREFIX)
from infra.extensions import r
from infra.logger import logger
from infra.keys import _touch_order, _get_free_station
from infra.mobius import _dispatch_order, _push_station_snapshot

from services import agv_fault, stock, collector
from services.process import set_stage, push_process

STATIONS = ("1", "2", "3")

# True  = 앞 주문이 재고로 막히면 건너뛰고 뒤 주문을 먼저 배정
# False = FIFO 엄수. 앞이 막히면 전부 대기
SKIP_BLOCKED = True


def pickup_occupied():
    """픽업대에서 AGV를 기다리는 주문(pickup_reached)이 있나.

    fault(pickup_failed 등)가 걸려 있어도 트레이는 그대로 픽업대에 있으므로 막는다.
    abort·타임아웃으로 끝나면 stage 가 completed 로 바뀌어 풀린다.
    """
    for sid in STATIONS:
        oid = r.get(f"{STATION_ORDER_PREFIX}{sid}")
        if oid and r.hget(f"order:{oid}", "stage") == "pickup_reached":
            return True
    return False


def try_assign_next():
    """큐 앞에서부터 훑어 처음으로 배치 가능한 주문 1건을 배정. 배정했으면 order_id."""
    if r.get(WAREHOUSE_KEY) != "idle" or r.get(WAREHOUSE_ORDER_KEY):
        return None
    if pickup_occupied():
        return None                 # 픽업대에 트레이가 있다. loaded 보고에서 다시 부른다
    if agv_fault.active():
        return None                 # 주문 없는 AGV 고장. 관리자 해제(/admin/agv/fault/clear)에서 다시 부른다

    station_id = _get_free_station()
    if not station_id:
        return None

    # lpop으로 꺼내면 건너뛸 수 없다. 훑고, 실제로 가져갈 것만 lrem 한다.
    for order_id in r.lrange(QUEUE_KEY, 0, -1):
        if not r.exists(f"order:{order_id}"):
            r.lrem(QUEUE_KEY, 1, order_id)
            logger.warning(f"[assign] 만료된 주문 정리 ({order_id})")
            continue

        blocked = stock.blockers(order_id)
        if not blocked:
            return _assign(order_id, station_id)

        logger.warning(f"[assign] {order_id} 보류 — {blocked}")
        r.hset(f"order:{order_id}", "blocked_by", json.dumps(blocked, ensure_ascii=False))
        if not SKIP_BLOCKED:
            return None

    return None


def _assign(order_id, station_id):
    okey = f"order:{order_id}"

    r.lrem(QUEUE_KEY, 1, order_id)
    r.hset(okey, "station_id", station_id)
    r.hdel(okey, "blocked_by")

    r.set(f"{STATION_ORDER_PREFIX}{station_id}", order_id)
    r.hset(STATION_KEY, station_id, "busy")

    r.set(WAREHOUSE_KEY, "busy")
    r.set(WAREHOUSE_ORDER_KEY, order_id)

    # 키캡 4개 기대 목록. 이게 있어야 cnt_keycap_dispense 콜백이 소진할 게 생긴다.
    # arm을 빼먹으면 dispense가 전부 unexpected로 떨어져 곧장 mismatched가 된다.
    collector.arm(order_id, "keycap", stock.parts_of(order_id, "keycap"))
    collector.reset_gate(order_id)

    _touch_order(order_id)          # 상태 키는 TTL 없음. 주문 키만 수명을 갱신한다

    # 명령은 cnt_process 하나로만 나간다. 창고·AGV는 그것만 보고 움직이므로
    # '배정이 실제로 전달됐는가'의 판정도 cnt_process push 성공 여부다.
    # 실패하면 배정을 통째로 되돌린다 — 창고가 못 받은 주문을 진행 중으로 두면 안 된다.
    if not set_stage(order_id, "assigned"):
        logger.error(f"[assign] cnt_process push 실패 → 배정 롤백 ({order_id})")
        _rollback(order_id, station_id)
        return None

    logger.info(f"[assign] {order_id} → station {station_id}")
    # 아래 둘은 기록용이다. 실패해도 로그만 남기고 진행한다 (명령이 아니므로).
    _dispatch_order(order_id)           # cnt_order — 배정 시점 기록
    _push_station_snapshot()            # cnt_station — 조립대 배정 변경 기록
    return order_id


def _rollback(order_id, station_id):
    from services import watchdog

    watchdog.disarm(order_id)           # set_stage가 걸어둔 assigned 마감을 푼다
    r.set(WAREHOUSE_KEY, "idle")
    r.delete(WAREHOUSE_ORDER_KEY)
    r.hset(STATION_KEY, station_id, "idle")
    r.delete(f"{STATION_ORDER_PREFIX}{station_id}")
    collector.clear(order_id, "keycap")
    collector.reset_gate(order_id)
    if r.exists(f"order:{order_id}"):
        r.hset(f"order:{order_id}", "stage", "queued")
        r.hdel(f"order:{order_id}", "station_id")
        r.lpush(QUEUE_KEY, order_id)                 # 큐 맨 앞으로
    # cnt_station push는 _assign 성공 후에만 일어난다. 롤백 시점엔 올린 게 없다.


def release_warehouse():
    """pickup_reached — 트레이가 픽업대에 도착해 벨트가 비었다.
    다음 배정은 픽업대 게이트에 막히고, AGV가 싣는(loaded) 순간 풀린다."""
    r.set(WAREHOUSE_KEY, "idle")
    r.delete(WAREHOUSE_ORDER_KEY)
    return try_assign_next()


def release_station(station_id):
    """completed / unclaimed — 조립대를 비운다.

    배정이 안 되면 cnt_process를 따로 밀지 않는다. set_stage(completed/unclaimed) 시점엔
    조립대가 아직 안 비어서 그 주문이 스냅샷에 남지만, station:current_order가 지워졌으므로
    '다음 push'에서 active_order_ids()에 안 잡혀 자동으로 빠진다.
    살아있는 구독자는 seq가 안 오르면 무시하고, completed는 어느 머신의 명령도 아니다.
    """
    r.delete(f"{STATION_ORDER_PREFIX}{station_id}")
    r.hset(STATION_KEY, str(station_id), "idle")
    return try_assign_next()


def on_stock_recovered():
    """카트리지가 empty/disable에서 정상으로 돌아왔을 때.
    이게 없으면 재고를 채워도 blocked_by가 붙은 주문이 다음 종료까지 대기한다."""
    assigned = try_assign_next()
    if assigned is None:
        push_process()          # 배정이 안 돼도 restock 목록은 갱신해서 내보낸다
    return assigned
