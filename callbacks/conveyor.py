"""cnt_conveyor/* 콜백.

    cnt_keycap_conveyor   {order_id, section01|section02: done|failed}
                                                               section02 done → keycap_reached
                                                               failed → keycap_0N_belt_jammed
                          {order_id, keycap1~4: done|failed}   이전 형식. 전부 done → keycap_reached
    cnt_tray_conveyor     {order_id, section1|section2: done}  section1 → tray_reached
                                                               section2 → pickup_reached
"""
from config import WAREHOUSE_ORDER_KEY
from infra.extensions import r
from infra.logger import logger

from services import collector, stock
from services.process import set_stage, set_fault, is_at_or_past

KEYCAP_FIELDS = ("keycap1", "keycap2", "keycap3", "keycap4")
KEYCAP_SECTIONS = (("section01", "01"), ("section02", "02"))


def _keycap_result(con):
    """확정본 con 은 {"order_id", "status": {"keycap1".."keycap4"}} 로 한 겹 안에 있다.
    최상위에 그대로 싣는 경우도 받아둔다 — 창고 구현이 어느 쪽이든 깨지지 않게."""
    inner = con.get("status")
    return inner if isinstance(inner, dict) else con


def handle_conveyor(path, con):
    match path:
        case ["cnt_keycap_conveyor"]:
            on_keycap_conveyor(con)
        case ["cnt_tray_conveyor"]:
            on_tray_conveyor(con)
        case _:
            logger.warning(f"[conveyor] 미정의 경로: {'/'.join(path)}")


def _order(con, tag):
    """진행 중 창고 주문과 대조. (order_id, stage) 또는 None."""
    order_id = r.get(WAREHOUSE_ORDER_KEY)
    if not order_id:
        logger.warning(f"[{tag}] 진행 중 주문 없음 — 무시")
        return None
    recv = con.get("order_id")
    if recv and recv != order_id:
        logger.warning(f"[{tag}] order_id 불일치 recv={recv} cur={order_id}")
        return None
    stage, fault = r.hmget(f"order:{order_id}", "stage", "fault")
    if fault:
        logger.warning(f"[{tag}] fault={fault} 정지 중 — 무시")
        return None
    return order_id, stage


def on_keycap_conveyor(con):
    got = _order(con, "keycap_conveyor")
    if got is None:
        return
    order_id, stage = got

    result = _keycap_result(con)
    if any(field in result for field, _ in KEYCAP_SECTIONS):
        for field, section in KEYCAP_SECTIONS:
            if result.get(field) == "failed":
                logger.error(f"[keycap_conveyor] {order_id} {field} 실패")
                set_fault(order_id, f"keycap_{section}_belt_jammed",
                          section=f"keycap_conveyor:{section}")
                return
        # section02 가 마지막 구간이다. section01 done 은 아직 이송 중이다.
        if result.get("section02") != "done":
            logger.info(f"[keycap_conveyor] {order_id} 진행 중 — {con}")
            return
    else:
        # 이전 형식(keycap1~4). 구간 정보가 없으면 01 로 기록한다.
        failed = [f for f in KEYCAP_FIELDS if result.get(f) == "failed"]
        if failed:
            section = str(con.get("section") or "01").zfill(2)
            logger.error(f"[keycap_conveyor] {order_id} 실패 {failed} section={section}")
            set_fault(order_id, f"keycap_{section}_belt_jammed",
                      section=f"keycap_conveyor:{section}")
            return

        if not all(result.get(f) == "done" for f in KEYCAP_FIELDS):
            logger.info(f"[keycap_conveyor] {order_id} 진행 중 — {con}")
            return

    if is_at_or_past(stage, "keycap_reached"):
        return                                   # 재전송
    set_stage(order_id, "keycap_reached")


def on_tray_conveyor(con):
    got = _order(con, "tray_conveyor")
    if got is None:
        return
    order_id, stage = got

    for section, ok_stage, fault, part in (
        ("section1", "tray_reached", "tray_01_belt_jammed", "tray_conveyor:01"),
        ("section2", "pickup_reached", "tray_02_belt_jammed", "tray_conveyor:02"),
    ):
        value = con.get(section)
        if value is None:
            continue
        if value == "failed":
            logger.error(f"[tray_conveyor] {order_id} {section} 실패")
            set_fault(order_id, fault, section=part)
            return
        if value != "done" or is_at_or_past(stage, ok_stage):
            continue

        if ok_stage == "tray_reached":
            # 보드 기대 목록을 여기서 깐다. 배정 때는 키캡만 깔았다.
            collector.arm(order_id, "board", stock.parts_of(order_id, "board"))
            set_stage(order_id, "tray_reached")
        else:
            set_stage(order_id, "pickup_reached")
            # 벨트가 비었다 → 창고 해제 + 다음 주문 배정
            from services.dispatch import release_warehouse
            release_warehouse()
        return

    logger.info(f"[tray_conveyor] {order_id} 처리할 section 없음 — {con}")
