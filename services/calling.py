"""호출 화면(calling app)이 그릴 내용을 한 번에 내려준다.

화면은 응답을 그대로 그리기만 한다. 어떤 번호를 어디에 띄울지는 여기서 정한다.
    arrived 전          → waiting (대기열에 있든 조립대에 배정됐든 손님에겐 똑같이 대기)
    arrived             → 호출 칸 calling
    verified, received  → 호출 칸 assembling
    unclaimed, completed → 어디에도 안 띄운다 (폐기 중이면 조립대는 잡혀 있어도 idle)
주문 하나는 stage 로 한 곳에만 분류되므로 호출 칸과 waiting 에 겹쳐 뜨지 않는다.
"""
from config import (QUEUE_KEY,
                    STAGE_ORDER,
                    STATION_ORDER_PREFIX,
                   )

from infra.extensions import r
from infra.logger import logger

STATIONS = ("1", "2", "3")

WAITING_STAGES = set(STAGE_ORDER[:STAGE_ORDER.index("arrived")])
STATION_STATUS = {"arrived": "calling",
                  "verified": "assembling",
                  "received": "assembling"}
DONE_STAGES = {"unclaimed", "completed"}


def _read_orders(order_ids):
    """order_id → (order_seq, stage). 해시가 TTL 로 사라진 주문은 (None, None)."""
    pipe = r.pipeline()
    for order_id in order_ids:
        pipe.hmget(f"order:{order_id}", "order_seq", "stage")
    rows = pipe.execute()
    return {order_id: (int(seq) if seq else None, stage)
            for order_id, (seq, stage) in zip(order_ids, rows)}


def get_display_status():
    # 대기열과 조립대는 MULTI 로 같은 시점을 읽는다. stage 는 그 뒤에 읽지만
    # 분류는 stage 하나로 하므로 그 사이 주문이 움직여도 두 곳에 뜨지는 않는다.
    pipe = r.pipeline()
    pipe.lrange(QUEUE_KEY, 0, -1)
    for station_id in STATIONS:
        pipe.get(f"{STATION_ORDER_PREFIX}{station_id}")
    queue, *held = pipe.execute()

    orders = _read_orders(list(dict.fromkeys(queue + [oid for oid in held if oid])))

    def known(order_id):
        seq, stage = orders[order_id]
        if seq is None or stage is None:
            return False
        if stage in WAITING_STAGES or stage in STATION_STATUS or stage in DONE_STAGES:
            return True
        logger.warning(f"[display] 알 수 없는 stage 표시 안 함 ({order_id}: {stage})")
        return False

    stations = []
    waiting = set()
    for station_id, order_id in zip(STATIONS, held):
        seq, status = None, "idle"
        if order_id and known(order_id):
            order_seq, stage = orders[order_id]
            if stage in STATION_STATUS:
                seq, status = order_seq, STATION_STATUS[stage]
            elif stage in WAITING_STAGES:
                waiting.add(order_seq)
        stations.append({"station_id": int(station_id), "order_seq": seq, "status": status})

    for order_id in queue:
        if known(order_id) and orders[order_id][1] in WAITING_STAGES:
            waiting.add(orders[order_id][0])

    return {"stations": stations, "waiting": sorted(waiting)}
