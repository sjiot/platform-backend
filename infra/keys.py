from datetime import datetime

from infra.extensions import r

from config import (QUEUE_KEY,
                    WAREHOUSE_KEY,
                    STATION_KEY,
                    AGV_KEY,
                    WAREHOUSE_ORDER_KEY,
                    STATION_ORDER_PREFIX,
                    MAX_QUEUE_LEN,
                    STATION_VERIFIED_PREFIX,
                    ORDER_COUNTER_KEY,
                    ORDER_PAGE_BASE,
                    STATION_RESET_PASSWORD,
                    MBTI_AXES,
                    ORDER_TTL,
                    ORDER_COUNTER_TTL,
                    COLOR_LIST,
                    BOARD_LIST,
                    KST,
                    STAGE_ORDER
                   )

def _order_counter_key():
    today = datetime.now(KST).strftime("%Y%m%d")
    key = f"{ORDER_COUNTER_KEY}:{today}"
    if r.exists(key):
        r.expire(key, ORDER_COUNTER_TTL)     # 예전엔 TTL 이 없어 날짜마다 영구히 쌓였다
    return key

def _order_keys(order_id):
    """한 주문이 만드는 키 전부. 만료가 어긋나면 죽은 주문의 pending 이 남아
    다음 주문 판정을 오염시키므로 항상 한 묶음으로 다룬다."""
    return [f"order:{order_id}",
            *[f"order:{order_id}:{slot}:{kind}"
              for slot in ("keycap", "board") for kind in ("pending", "wrong")],
            f"order:{order_id}:gate"]


def _touch_order(order_id, ttl=ORDER_TTL):
    """주문 스코프 키의 수명을 한 번에 갱신한다 (기본 ORDER_TTL = 24시간).

    상태 키(warehouse·station·agv·queue·process)에는 TTL 을 걸지 않는다.
    그건 '지금 상태'라 만료되면 안 된다 — 예전엔 TEST_KEY_TTL 하나로 여기까지 TTL 이
    걸려서, 한 시간 조용하면 warehouse:occupancy 가 사라졌다.
    상태 키를 지우는 경로는 POST /admin/reset 하나뿐이다.
    """
    for key in _order_keys(order_id):
        if r.exists(key):
            r.expire(key, ttl)


def _ensure_initial_state():
    """warehouse/station 상태키가 없으면 초기값으로 세팅"""
    if not r.exists(WAREHOUSE_KEY):
        r.set(WAREHOUSE_KEY, "idle") # 키캡별로, 로봇팔 설정 필요
    if not r.exists(STATION_KEY):
        r.hset(STATION_KEY, mapping={"1": "idle", "2": "idle", "3": "idle"})
    if not r.exists(AGV_KEY):
        r.set(AGV_KEY, "idle")

def _get_free_station():
    station_status = r.hgetall(STATION_KEY)
    return next((sid for sid in ("1", "2", "3") if station_status.get(sid, "idle") == "idle"), None)

