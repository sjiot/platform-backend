"""주문에 붙일 수 없는 AGV 고장 — cnt_process 최상위 agv_fault.

AGV fault 는 두 갈래로 나간다.
  - 대상 주문을 아는 것 : 창고와 같이 orders.{order_id}.fault (services/process.set_fault)
  - 대상 주문이 없는 것 : 여기. 빈 차로 복귀하다 고장 난 경우 등
                          cnt_process 의 "agv_fault": [{fault, fault_section, at}]

고장이 없으면 agv_fault 는 null 이다. 주문 fault 와 같은 규칙으로 'null = 해결'로 읽는다.
풀리는 경로는 관리자 해제(POST /admin/agv/fault/clear) 하나뿐이다 — parked 같은 정상 보고는
평소에도 오므로 회복 신호로 쓰지 않는다. 걸려 있는 동안 배정·새 주문 접수를 막는다.
"""
from config import AGV_FAULT_KEY, AGV_STATUS_KEY
from infra.extensions import r
from services.device_fault import DeviceFaults

# cnt_agv status → fault
FAULTS = {
    "load_failed": "pickup_failed",
    "unload_failed": "drop_failed",
    "agv_broken": "agv_broken",
    # AGV 는 fault 이름을 status 로 그대로 싣는다 (2026-11-12 실측: status=pickup_failed).
    # 명세 이름(load_failed·unload_failed)과 둘 다 받는다.
    "pickup_failed": "pickup_failed",
    "drop_failed": "drop_failed",
}

SECTION = "agv"

_faults = DeviceFaults(AGV_FAULT_KEY, "agv_fault")
items = _faults.items
active = _faults.active
clear = _faults.clear


def add(fault):
    _faults.add(fault, SECTION, field=fault)


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
