import json
from datetime import datetime, timezone, timedelta

from infra import latency
from infra.extensions import r
from infra.logger import logger
from infra.keys import _touch_order
from infra.mobius import create_cin

from config import KST, STAGE_ORDER, STATION_ORDER_PREFIX, WAREHOUSE_ORDER_KEY
from services import agv_fault, collector, stock, warehouse_fault, watchdog

_ALIAS = {
    "keycap_mismatched": "assigned",
    "board_mismatched": "tray_reached",
    "unclaimed": "arrived",
}
_RANK = {s: i for i, s in enumerate(STAGE_ORDER)}


def rank(stage):
    return _RANK.get(_ALIAS.get(stage, stage), -1)

def is_at_or_past(stage, target):
    """이미 그 단계를 지났는가 — 재전송 알림을 거르는 가드."""
    return rank(stage) >= rank(target)


# ── 라우트 가드용 원본 순번 ──────────────────────────────────────────
# rank()는 _ALIAS로 unclaimed를 arrived에 접는다. 재전송을 거를 땐 그게 맞지만
# (노쇼는 arrived의 곁가지지 그 다음 단계가 아니다), 라우트 가드는 둘을 구분해야 한다.
# unclaimed 주문에 /start가 들어오면 rank 기준으론 arrived와 동급이라 그냥 통과한다.
# 그래서 가드는 STAGE_ORDER 배열 순서를 그대로 쓴다.

def stage_index(stage):
    """STAGE_ORDER 원본 순번. 모르는 값은 -1."""
    return _RANK.get(stage, -1)

def is_before(stage, target):
    """STAGE_ORDER 기준 target보다 앞인가. stage가 None이면 True(아직 안 왔다)."""
    return stage_index(stage) < stage_index(target)

def is_after(stage, target):
    """STAGE_ORDER 기준 target보다 뒤인가."""
    return stage_index(stage) > stage_index(target)

TERMINAL = {"completed", "unclaimed"}


def set_stage(order_id, stage):
    """stage 전이 + cnt_process push. 콜백은 항상 이걸로만 stage를 바꾼다."""
    key = f"order:{order_id}"
    prev = r.hget(key, "stage")
    r.hset(key, "stage", stage)
    _touch_order(order_id)
    logger.info(f"[stage] {order_id} {prev} -> {stage}")
    latency.record("order_stage", 0, order_id=order_id, stage=stage, prev=prev)

    if stage in TERMINAL:
        watchdog.disarm(order_id)
    else:
        watchdog.arm(order_id, stage)

    # push 성공 여부를 돌려준다 — 명령 채널이 cnt_process 하나뿐이라
    # '창고가 이 주문을 받았는가'의 판정 근거가 이것밖에 없다 (dispatch._assign).
    return push_process()

def set_fault(order_id, fault, section=None):
    """fault만 세우고 stage는 그대로 둔다
    관리자가 fault를 지우면 그 자리에서 이어진다.

    section 은 '어디가 문제냐' — fault 가 '무엇이 고장났나'이므로 두 축을 한 문자열에 섞지 않는다.
    형식은 part[:scope[:slot]] (services/stock.py::section_of). 없으면 지운다 —
    앞 fault 의 section 이 남으면 엉뚱한 부품을 가리킨다.
    """
    key = f"order:{order_id}"
    if section:
        r.hset(key, mapping={"fault": fault, "fault_section": section})
    else:
        r.hset(key, "fault", fault)
        r.hdel(key, "fault_section")
    _touch_order(order_id)
    watchdog.disarm(order_id)
    logger.error(f"[fault] {order_id} <- {fault}" + (f" @{section}" if section else "")
                 + f" (stage={r.hget(key, 'stage')} 유지)")
    push_process()

# 해제할 때 재고 상태까지 되돌려야 하는 fault. 나머지는 fault 필드만 지우면 된다.
_REVIVABLE = {"keycap_driver_failed", "keycap_cartridge_failed"}


def _shelves_to_revive(order_id, section):
    """되살릴 선반 집합.

    order 해시에는 fault 가 한 칸뿐이라 선반 둘이 연달아 죽으면 뒤엣것이 앞엣것을 덮는다
    (ENTP 주문에서 E 드라이버와 P 드라이버가 같이 죽는 경우가 그렇다). fault_section 만
    믿으면 P 만 살리고 E 는 차단된 채로 남는다. 그래서 warehouse:driver_failed 에 아직
    남아 있는 선반 중 **이 주문이 쓰는 것**을 같이 본다 — 남의 주문 때문에 죽은 선반은
    교집합에서 빠지므로 건드리지 않는다.
    """
    parts = (section or "").split(":")
    shelves = {parts[1]} if len(parts) > 1 and parts[1] in stock.SHELVES else set()
    mine = {stock.shelf_of(s) for s in stock.parts_of(order_id, "keycap")}
    return shelves | (mine & stock.driver_failed_shelves())


def _revive_for(order_id, fault, section):
    """해제한 fault 가 막아둔 칸만 되살린다. 관계없는 disable 은 그대로 둔다."""
    if fault not in _REVIVABLE:
        return

    if fault == "keycap_driver_failed":
        shelves = _shelves_to_revive(order_id, section)
        if not shelves:
            logger.warning(f"[fault] {order_id} {fault} 인데 되살릴 선반을 못 찾았다 "
                           f"(section={section!r}) — 재고 상태는 그대로 둔다")
            return
        for shelf in sorted(shelves):
            revived = stock.revive_slots(stock.slots_of_shelf(shelf))
            stock.clear_driver_failed(shelf)
            logger.info(f"[fault] {order_id} 선반 {shelf} 차단 해제 · disable→idle {revived}")
        return

    # keycap_cartridge_failed — 칸 하나. section 의 세 번째 토큰이 그 칸이다
    # (keycap_cartridge:keycap1:E_r · services/stock.py::section_of).
    parts = (section or "").split(":")
    slot = parts[2] if len(parts) > 2 else None
    if not slot:
        logger.warning(f"[fault] {order_id} {fault} 인데 section 에 칸이 없다 "
                       f"(section={section!r}) — 재고 상태는 그대로 둔다")
        return
    logger.info(f"[fault] {order_id} disable→idle {stock.revive_slots([slot])}")


def clear_fault(order_id):
    """관리자 해제 — stage는 그대로이므로 그 자리에서 이어지고, 시계만 다시 건다.

    해제는 '고쳤다'는 선언이다. fault 필드만 지우면 두 가지가 어긋난 채로 남는다.

      1) fault 가 걸려 있는 동안 들어온 배출 콜백을 가드(_current_order)가 전부 버렸다.
         collector 의 pending 이 실제 배출과 달라서, 해제해도 consume 이 done 에 닿지
         못하고 그 stage 에 그대로 선다.
      2) 그 fault 가 남긴 칸 disable · 선반 차단이 그대로라 다음 주문이 blockers 에서 막힌다.

    둘 다 fault 를 지우기 **전에** 되돌린다 — 지우고 나면 어느 칸이었는지 알 방법이 없다.
    """
    key = f"order:{order_id}"
    fault, section, stage = r.hmget(key, "fault", "fault_section", "stage")

    _revive_for(order_id, fault, section)

    # 재기동 복구와 같은 문제(배출분을 모르는 채로 pending 재구성)라 구현을 둘로 두지 않는다.
    # remove_only — 콜백이 소진해 둔 pending 이 기준이고, Mobius 는 빼는 데만 쓴다.
    # recovery 가 process 를 쓰므로 순환 import 회피용 지연 import.
    from services.recovery import _restore_collector
    _restore_collector(order_id, stage, tag="fault", remove_only=True)

    r.hdel(key, "fault", "fault_section")
    _touch_order(order_id)
    if (section or "").startswith(agv_fault.SECTION):
        agv_fault.clear_status_if_fault()   # agv:status 에 load_failed 등이 남아 고장으로 보이지 않게

    if _resume_stage(order_id, stage):
        return                          # set_stage 가 시계·push 까지 했다

    watchdog.arm(order_id, stage)
    push_process()


def clear_faults_at(slot):
    """관리자가 칸을 되살렸다(/admin/stock/revive) — 그 자리를 가리키는 주문 fault 를 같이 푼다.
    푼 주문 id 목록을 돌려준다.

    칸 고장 해제와 주문 fault 해제는 같은 '고쳤다' 선언이다. 칸만 idle 로 돌리고 주문 fault 를
    두면 cnt_process 에 fault·fault_section 이 계속 실려 창고는 여전히 멈춘 것으로 본다
    (실측: E_r disable→idle 뒤에도 keycap_cartridge_failed @keycap1:E_r 이 그대로 나갔다).

    대상은 fault_section 이 그 칸(keycap_cartridge:keycap1:E_r)이거나 그 칸의 선반
    (keycap_cartridge:keycap1 — driver_failed)인 주문. 선반 고장은 칸 하나를 눌러도 '선반을
    고쳤다'로 본다 — revive 가 그 선반의 driver_failed 표시를 같이 지우는 것과 같은 판단이다.
    clear_fault 가 collector 재동기화·push_process 까지 한다.
    """
    targets = {stock.section_of(slot)}
    if not stock.is_board(slot):
        targets.add(stock.section_of(slot, shelf_only=True))

    cleared = []
    for oid in _faultable_order_ids():
        fault, section = r.hmget(f"order:{oid}", "fault", "fault_section")
        if not fault or section not in targets:
            continue
        logger.info(f"[fault] {oid} {fault} @{section} — {slot} 되살림에 따라 해제")
        clear_fault(oid)
        cleared.append(oid)
    return cleared


def _faultable_order_ids():
    """fault 가 걸릴 수 있는 주문 = 조립대에 배정된 주문 + 창고가 든 주문. queued 는 fault 가 없다."""
    ids = active_order_ids()
    oid = r.get(WAREHOUSE_ORDER_KEY)
    if oid and oid not in ids:
        ids.append(oid)
    return ids


def _resume_stage(order_id, stage):
    """fault 중에 이미 다 모였던 단계를 해제 시점에 이어서 넘긴다. 넘겼으면 True.

    콜백은 fault 중에도 collector 를 갱신하지만 stage 는 움직이지 않는다
    (callbacks/warehouse.py::_current_order). 그래서 마지막 1건이 fault 중에 들어오면
    '다 모였는데 전이만 안 된' 상태로 남는다 — 창고는 이미 다 냈으니 재배출이 올 일이
    없고, 여기서 넘기지 않으면 그 자리에 영원히 선다.

    근거는 Mobius 추측이 아니라 **우리가 받은 콜백으로 소진한 pending** 이다.
    오배출(wrong)이 하나라도 있으면 넘기지 않는다 — 그건 사람이 봐야 한다.
    """
    if stage not in ("assigned", "keycap_mismatched"):
        return False
    if not stock.parts_of(order_id, "keycap"):
        return False
    if collector.remaining(order_id, "keycap") or collector.wrong(order_id, "keycap"):
        return False

    logger.info(f"[fault] {order_id} 키캡이 이미 다 모였다 — keycap_dispensed 로 이어간다")
    set_stage(order_id, "keycap_dispensed")
    return True


# cnt_process 스냅샷
def active_order_ids():
    """진행 중 주문 = 조립대에 배정된 주문. queued는 스냅샷에 올리지 않는다."""
    oids = []
    for sid in ("1", "2", "3"):
        oid = r.get(f"{STATION_ORDER_PREFIX}{sid}")
        if oid:
            oids.append(oid)
    return oids

# 진행 주문이 없을 때 orders 에 넣는 표시. 주문 id 는 ord_ 로 시작하므로 겹치지 않는다.
OPEN_KEY = "open"


def _build_body(reset=False):
    """스냅샷의 '내용' 부분. seq·updated_at은 뺀다 — 매번 달라져서 비교가 안 되므로.

    진행 주문이 없으면 orders 안에 "open": true 를 싣는다 — {"orders": {"open": true}}.
    머신은 orders 가 {} 이면 하드 리셋하므로, 중단(abort)·완료로 주문이 다 빠진 것과
    관리자 리셋을 이 키로 가른다. orders 가 {} 로 나가는 건 POST /admin/reset 의
    push(reset=True) 한 번뿐이다. 주문이 있으면 open 은 싣지 않는다.
    orders 를 읽는 쪽은 값이 dict 가 아닌 키(OPEN_KEY)를 건너뛰어야 한다
    (recovery._restore_process · snapshot).
    스냅샷은 전량이라 중단 API 에서만 실으면 안 된다 — 다음 push(재고 콜백 등)가 open 없이
    다시 나가서 머신이 그걸 리셋으로 본다.
    """
    orders = {}
    for oid in active_order_ids():
        o = r.hgetall(f"order:{oid}")
        if not o:
            continue
        orders[oid] = {
            "station_id": o.get("station_id"),
            "order_seq": int(o.get("order_seq") or 0),
            "status": o.get("stage"),          # Redis stage → 스냅샷 status
            "board": o.get("board"),
            "keycap": o.get("keycap"),
            "colors": [c for c in (o.get("colors") or "").split(",") if c],
            "fault": o.get("fault") or None,
            "fault_section": o.get("fault_section") or None,   # 어느 부품·선반인지
        }

    # 주문에 붙일 수 없는 AGV·창고 고장. 없으면 null — 주문 fault 와 같이 'null = 해결'로 읽는다.
    # restock 목록과 달리 키를 늘 싣는다. 해제가 키 삭제가 아니라 null 로 보여야 하므로.
    body = {"orders": orders,
            "agv_fault": agv_fault.items(),
            "warehouse_fault": warehouse_fault.items()}
    if not orders and not reset:
        orders[OPEN_KEY] = True
    # 스냅샷은 전량이므로 수신측은 '키 없음 = 빈 목록'으로 읽어야 한다 
    keycap_restock = restock_list("keycap")
    if keycap_restock:
        body["keycap_restock"] = keycap_restock

    board_restock = restock_list("board")
    if board_restock:
        body["board_restock"] = board_restock

    return body

def push_process(periodic=False, reset=False):
    """진행 중 주문 전체를 통째로 push. 변경분만 보내면 머신이 상태를 들게 된다.

    내용이 **직전에 전송된** push와 같으면 아예 올리지 않는다.
      - 아무 데서나 push_process()를 불러도 안전하고 (재고 콜백 34개 포함)
      - seq가 '내용이 바뀔 때만' 오른다는 약속이 실제로 지켜진다.
      - 지문(process:last)은 create_cin 이 성공한 뒤에만 남긴다 — 아래 주석.

    periodic=True 는 주기 재전송용 — 내용이 그대로면 seq를 유지한 채 다시 올린다.
    수신측의 'seq 같으면 무시' 필터가 여기서 작동한다.

    reset=True 는 POST /admin/reset 전용 — 주문이 없어도 open 을 싣지 않는다 (_build_body).
    """
    body = _build_body(reset=reset)
    fingerprint = json.dumps(body, sort_keys=True, ensure_ascii=False)
    changed = fingerprint != r.get("process:last")

    if not changed and not periodic:
        latency.record("process_push_skipped", 0)
        return True

    if changed:
        seq = r.incr("process:seq")
    else:
        seq = int(r.get("process:seq") or 0)

    con = {
        "seq": seq,
        "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
        **body,
    }
    logger.info(f"[process] push seq={seq} orders={list(body['orders'])} "
                f"keycap_restock={body.get('keycap_restock')} board_restock={body.get('board_restock')}")
    ok = create_cin("cnt_process", con)

    # 지문(process:last)은 **전송에 성공한 뒤** 남긴다. 먼저 남기면 전송이 실패해도 '머신이 봤다'로
    # 기록돼, 같은 내용의 다음 push 가 중복으로 건너뛰어진다 — 배정 push 실패 → 롤백 → 재배정이
    # CIN 없이 성공 처리되고 창고는 stage_timeout 까지 주문을 모르던 경로가 그것이다 (dispatch._assign).
    # seq 는 이미 올라갔으므로 재전송은 번호 하나를 건너뛰고 나간다. 머신의 'seq 안 오르면 무시'
    # 필터는 번호가 비는 것은 상관하지 않는다. 그 사이 스냅샷 도구가 내는 'Redis seq 가 Mobius 보다
    # 앞선다' 경고가 정확히 이 상태다 (services/snapshot.py). document/flow-break-points.md 1번.
    if changed and ok:
        r.set("process:last", fingerprint)
    elif changed:
        logger.error(f"[process] push seq={seq} 전송 실패 — 지문을 남기지 않는다. "
                     f"다음 push 가 같은 내용을 다시 보낸다")
    return ok

def restock_list(kind):
    """채워야 할 칸 목록. 순서가 흔들리면 내용이 같은데 fingerprint가 달라지므로 stock에서 정렬해 준다."""
    return stock.restock_list(kind)
