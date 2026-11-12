"""주문에 붙일 수 없는 AGV 고장 — cnt_process 최상위 agv_fault.

AGV fault 는 두 갈래로 나간다.
  - 대상 주문을 아는 것 : 창고와 같이 orders.{order_id}.fault (services/process.set_fault)
  - 대상 주문이 없는 것 : 여기. 빈 차로 복귀하다 고장 난 경우 등
                          cnt_process 의 "agv_fault": [{fault, fault_section, at}]

고장이 없으면 agv_fault 는 null 이다. 주문 fault 와 같은 규칙으로 'null = 해결'로 읽는다.
풀리는 경로는 관리자 해제(POST /admin/agv/fault/clear) 하나뿐이다 — parked 같은 정상 보고는
평소에도 오므로 회복 신호로 쓰지 않는다. 걸려 있는 동안 배정·새 주문 접수를 막는다.
"""
import json
from datetime import datetime

from config import AGV_FAULT_KEY, AGV_STATUS_KEY, KST
from infra.extensions import r
from infra.logger import logger

# cnt_agv status → fault
FAULTS = {
    "load_failed": "pickup_failed",
    "unload_failed": "drop_failed",
    "agv_broken": "agv_broken",
}

SECTION = "agv"


def add(fault):
    """같은 fault 가 다시 오면 시각만 갱신한다 (재전송·반복 보고)."""
    item = {"fault": fault, "fault_section": SECTION,
            "at": datetime.now(KST).isoformat(timespec="seconds")}
    r.hset(AGV_FAULT_KEY, fault, json.dumps(item, ensure_ascii=False))
    logger.error(f"[agv_fault] 주문 없는 AGV 고장 <- {fault}")


def items():
    """cnt_process 에 실을 값. 없으면 None. fault 이름순 — 순서가 흔들리면 push 지문이 달라진다."""
    raw = r.hgetall(AGV_FAULT_KEY)
    if not raw:
        return None
    out = []
    for name in sorted(raw):
        try:
            out.append(json.loads(raw[name]))
        except (TypeError, ValueError):
            out.append({"fault": name, "fault_section": SECTION, "at": None})
    return out


def active():
    return bool(r.exists(AGV_FAULT_KEY))


def clear():
    """전부 지우고 지운 fault 이름 목록을 돌려준다. push 는 호출부가 한다."""
    names = sorted(r.hkeys(AGV_FAULT_KEY))
    r.delete(AGV_FAULT_KEY)
    return names


def clear_status_if_fault():
    """agv:status 가 고장 보고(load_failed…)로 남아 있으면 지운다. 해제 뒤에도 고장으로 보이지 않게.

    주문 없는 고장이 아직 남아 있으면 그대로 둔다 — 그 고장의 마지막 보고일 수 있다.
    """
    if active():
        return False
    if r.get(AGV_STATUS_KEY) in FAULTS:
        r.delete(AGV_STATUS_KEY)
        return True
    return False
