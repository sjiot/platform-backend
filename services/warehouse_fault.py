"""주문에 붙일 수 없는 창고 고장 — cnt_process 최상위 warehouse_fault.

창고 fault 는 두 갈래로 나간다.
  - 진행 주문이 그 칸을 아직 써야 할 때 : orders.{order_id}.fault (callbacks/warehouse.py)
  - 그 밖 (주문 없음 · 이미 지나감 · 다른 칸) : 여기.
        "warehouse_fault": [{fault, fault_section, at}]

fault · fault_section 은 주문 fault 와 같은 값을 쓴다.
  keycap_cartridge_failed  keycap_cartridge:keycap1:E_r   (칸 하나)
  keycap_driver_failed     keycap_cartridge:keycap1       (선반 전체)
empty(품절)와 board disable 은 올리지 않는다 — 품절은 /stock/out·blockers 가 맡고,
보드 보관대는 정의된 fault 가 없다.

풀리는 경로 셋.
  - 창고가 그 칸에서 idle·busy 를 다시 올림 (그 칸 + 그 선반 항목)
  - 관리자 POST /admin/stock/revive (칸 되살림과 함께)
  - 관리자 POST /admin/warehouse/fault/clear (항목만. 칸 상태는 그대로)
새 주문 접수(POST /order)는 keycap_driver_failed(선반 전체)가 걸려 있을 때만 막는다.
keycap_cartridge_failed(칸 하나)는 그 칸을 쓰는 조합만 막힌다 — 칸이 disable 이라 blockers 가
뒤로 민다. 배정은 어느 쪽도 막지 않는다.
"""
from config import WAREHOUSE_FAULT_KEY
from services import stock
from services.device_fault import DeviceFaults

_faults = DeviceFaults(WAREHOUSE_FAULT_KEY, "warehouse_fault")
items = _faults.items
active = _faults.active
clear = _faults.clear
remove = _faults.remove


# 걸려 있으면 새 주문 접수 전체를 막는 fault. 나머지는 그 칸만 blockers 가 막는다.
BLOCKS_INTAKE = {"keycap_driver_failed"}


def blocks_intake():
    """새 주문 접수를 막아야 하는 창고 고장이 있나 (routes/order.require_no_fault)."""
    return any(item.get("fault") in BLOCKS_INTAKE for item in (items() or []))


def add(fault, section):
    _faults.add(fault, section, field=section)


def clear_slot(slot):
    """그 칸과 그 칸의 선반 항목을 지운다. 지운 section 목록을 돌려준다.

    선반 항목까지 같이 지우는 건 stock.clear_driver_failed 와 같은 판단이다 — 선반의 어느 칸이든
    정상 status 를 올렸다 = 그 선반 드라이버가 살아 있다.
    """
    if stock.is_board(slot):
        return []
    sections = [stock.section_of(slot)]
    if stock.shelf_of(slot):
        sections.append(stock.section_of(slot, shelf_only=True))
    return _faults.remove(sections)
