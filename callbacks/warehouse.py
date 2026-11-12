from config import WAREHOUSE_ORDER_KEY
from infra.extensions import r
from infra.logger import logger

from services import collector, stock, warehouse_fault
from services.stock import BOARD_SLOT_MAP as BOARD_CODE
from services.process import set_stage, set_fault, push_process

# 이 콜백이 정상적으로 올 수 있는 stage. 그 외는 재전송으로 보고 버린다.
KEYCAP_STAGES = {"assigned", "keycap_mismatched"}
BOARD_STAGES = {"tray_reached", "board_mismatched"}

MAX_RETRY = 2


def handle_warehouse(path, con):
    """path 는 라우터가 'cnt_warehouse'를 뗀 나머지.
        ['cnt_keycap_dispense']
        ['cnt_keycap_status', 'cnt_E_r']
    """
    match path:
        case ["cnt_keycap_dispense"]:
            on_keycap_dispense(con)
        case ["cnt_board_dispense"]:
            on_board_dispense(con)
        case ["cnt_keycap_status", cnt_rn]:
            on_cartridge_status("keycap", cnt_rn.removeprefix("cnt_"), con)
        case ["cnt_board_status", cnt_rn]:
            on_cartridge_status("board", cnt_rn.removeprefix("cnt_"), con)
        case _:
            logger.warning(f"[warehouse] 미정의 경로: {'/'.join(path)}")


# 공통 가드
def _current_order(con, allow_stages, tag):
    """(order_id, stage, faulted) 또는 None.

    가드 3단 — 진행 중 주문 있음 / order_id 일치 / stage 맞음.
    stage 가드가 재전송·유령 알림을 거른다.

    **fault 는 여기서 거르지 않는다.** 배출 CIN 은 '부품이 물리적으로 나왔다'는 사실이라
    기록은 언제나 옳고, 위험한 건 그걸 근거로 다음 단계로 넘어가는 쪽이다.
    fault 라고 통째로 버리면 그 사이 **멀쩡한 선반이 낸 배출까지 사라진다** —
    driver_failed 는 선반 하나만 죽이므로 나머지 선반은 계속 배출하는데, 그게 유실되면
    해제 후 pending 이 영원히 안 비워진다 (실측: E·T 고장 중 N_r·P_r 이 통째로 무시됐다).

    faulted=True 면 호출부는 collector 만 갱신하고 stage 전이는 하지 않는다.
    다 모인 채로 멈춘 경우는 해제 시점에 process.clear_fault 가 이어서 넘긴다.
    """
    order_id = r.get(WAREHOUSE_ORDER_KEY)
    if not order_id:
        logger.warning(f"[{tag}] 진행 중 창고 주문 없음 — 무시")
        return None

    recv = con.get("order_id")
    if recv and recv != order_id:
        logger.warning(f"[{tag}] order_id 불일치 recv={recv} cur={order_id}")
        return None

    stage, fault = r.hmget(f"order:{order_id}", "stage", "fault")
    if stage not in allow_stages:
        logger.warning(f"[{tag}] stage={stage} (기대 {sorted(allow_stages)}) — 무시")
        return None

    return order_id, stage, bool(fault)


def _mismatch(order_id, slot):
    """불일치 — 재출고 2회까지, 초과하면 종료."""
    n = r.hincrby(f"order:{order_id}", f"{slot}_retry", 1)
    left = collector.retry(order_id, slot)   # 오배출 기록만 비우고 부족분을 남긴다

    if n > MAX_RETRY:
        logger.error(f"[{slot}] 재출고 {MAX_RETRY}회 초과 — 주문 종료 필요 (부족분 {left})")
        # TODO: finish_order(order_id, "completed", reason=f"{slot}_mismatch") 연결
        # 못 받은 칸이 문제의 지점이다. 부족분이 여러 개면 첫 칸을 싣고 전체는 위 로그에 남는다.
        short = left or stock.parts_of(order_id, slot)
        section = stock.section_of(short[0]) if short else None
        set_fault(order_id, f"{slot}_cartridge_failed", section=section)
        return

    logger.warning(f"[{slot}] 불일치 {n}회차 — 부족분 {left} 재출고")
    set_stage(order_id, f"{slot}_mismatched")


# 키캡 배출 (4건)
def on_keycap_dispense(con):
    """con = {"order_id": "ord_1234", "keycap": "E_r"} — 주문당 4건.

    배정 시점에 깔아둔 pending 4개를 하나씩 지운다.
    다 지워지기 전에는 Redis만 갱신하고 끝 — cnt_process는 나가지 않는다.
    """
    got = _current_order(con, KEYCAP_STAGES, "keycap_dispense")
    if got is None:
        return
    order_id, _, faulted = got

    keycap = con.get("keycap")
    if not keycap:
        logger.warning("[keycap_dispense] keycap 필드 없음")
        return

    result, left = collector.consume(order_id, "keycap", keycap)
    logger.info(f"[keycap_dispense] {order_id} {keycap} → {result} 남은={left}")

    if faulted:
        # 기록은 위에서 끝났다. fault 중에는 stage 를 움직이지 않는다 —
        # _mismatch 도 set_stage 를 부르므로 같이 보류한다 (오배출 기록은 consume 이 이미 남겼다).
        # done 이었다면 해제 시점에 clear_fault 가 keycap_dispensed 로 이어간다.
        logger.warning(f"[keycap_dispense] {order_id} fault 중 — {result} 기록만, 전이 보류")
        return

    match result:
        case "waiting":
            return                                    # 아직 안 모임. 여기서 끝.
        case "done":
            set_stage(order_id, "keycap_dispensed")   # hset + _touch + cnt_process push
        case "unexpected":
            _mismatch(order_id, "keycap")


# 보드 배출 (1건)
def on_board_dispense(con):
    """con = {"order_id": "ord_1234", "board": "blue"} — 주문당 1건.

    board_packed는 이 색 대조 + cnt_board_robot_arm 의 done, 둘 다 있어야 한다(명세 6-2).
    그래서 여기서는 플래그만 세우고 완성 판정은 try_board_packed에 맡긴다.
    """
    got = _current_order(con, BOARD_STAGES, "board_dispense")
    if got is None:
        return
    order_id, _, faulted = got

    board = con.get("board")
    if not board:
        logger.warning("[board_dispense] board 필드 없음")
        return

    result, left = collector.consume(order_id, "board", BOARD_CODE.get(board, board))
    logger.info(f"[board_dispense] {order_id} {board} → {result}")

    if result == "done":
        collector.mark(order_id, "board_dispensed")   # 색 대조 성공은 사실이다. 기록은 남긴다

    if faulted:
        logger.warning(f"[board_dispense] {order_id} fault 중 — {result} 기록만, 전이 보류")
        return

    match result:
        case "waiting":
            return
        case "done":
            try_board_packed(order_id)                # 플래그 2개가 다 모였는지는 여기서 본다
        case "unexpected":
            _mismatch(order_id, "board")


def try_board_packed(order_id):
    """dispense 색 일치 + robot_arm done. 늦게 온 쪽이 완성을 확인한다.
    callbacks/robot_arm.py 의 on_board_robot_arm 에서도 이 함수를 부른다."""
    if collector.has_all(order_id, "board_dispensed", "board_placed"):
        set_stage(order_id, "board_packed")


# 카트리지 상태
def _still_needs(order_id, kind):
    """이 주문이 그 칸을 아직 잡고 있는(=배출 전) 구간인가.

    마지막 1개가 나가서 empty가 된 경우는 fault가 아니다 — 그 부품은 이미 트레이 위에 있고,
    빈 카트리지는 '다음 주문'의 문제다. 예약 해제 시점과 같은 집합을 쓴다 (명세 8-1).
    """
    stage = r.hget(f"order:{order_id}", "stage")
    reserved = stock.KEYCAP_RESERVED if kind == "keycap" else stock.BOARD_RESERVED
    return stage in reserved


def on_cartridge_status(kind, cartridge_id, con):
    """con = {"order_id": ..., "count": 6, "status": "idle"}

    stage와 무관하다. 배출 시작(busy)·완료(idle)로 주문당 2회씩 올라오고,
    진행 중 주문이 없을 때도 온다. 재고는 무조건 덮어쓴다.
    """
    count = con.get("count")
    status = con.get("status")
    prev = stock.get(cartridge_id) or {}
    prev_status = prev.get("status")
    prev_count = int(prev.get("count") or 0)
    stock.put(cartridge_id, count, status)
    logger.info(f"[{kind}_status] {cartridge_id} count={count} status={status}")

    # 관리자가 채운 칸을 창고가 반영했다(count>0) → restock 목록에서 뺀다.
    # 목록이 바뀌었으니 아래 어느 갈래로 가든 push_process 가 새 seq 로 올린다.
    if int(count or 0) > 0 and stock.clear_restocked(cartridge_id):
        logger.info(f"[{kind}_status] {cartridge_id} 채움 반영 확인 — restock 목록에서 제거")

    if status in ("idle", "busy"):
        # 막혀 있던 칸이 풀렸다 → 보류된 주문을 다시 시도한다.
        # 이게 없으면 재고를 채워도 blocked_by 주문이 다음 종료까지 대기한다.
        # status만 보면 안 된다 — 배출로 count가 0이 된 칸은 status가 idle인 채로 막히므로
        # (blockers는 count<=0도 막는다) 채워 넣어도 복구 트리거가 안 걸린다.
        # 그 선반에서 정상 status 가 올라왔다 = 드라이버가 살아났다. 선반 차단을 푼다.
        shelf = stock.shelf_of(cartridge_id)
        revived = bool(stock.clear_driver_failed(shelf))
        if revived:
            logger.info(f"[{kind}_status] 선반 {shelf} 드라이버 복구 — 8칸 차단 해제")
        # 주문 없는 창고 고장도 같은 판단으로 푼다 — 그 칸 + 그 선반 항목.
        # push 는 아래 두 갈래가 모두 한다 (on_stock_recovered 도 배정이 없으면 push).
        if warehouse_fault.clear_slot(cartridge_id):
            logger.info(f"[{kind}_status] {cartridge_id}={status} — 주문 없는 창고 고장 해제")
        if revived or (int(count or 0) > 0
                       and (prev_status in stock.BLOCKING_STATUS or prev_count <= 0)):
            from services.dispatch import on_stock_recovered
            on_stock_recovered()
        else:
            # count 0 은 restock 목록을 바꾸지 않는다 — 관리자가 채워야 들어간다.
            # 위에서 목록이 바뀌었으면 여기서 나간다. 내용이 같으면 push_process가 안 올린다.
            push_process()
        return

    # 정상(idle·busy)도 고장(disable·empty)도 아닌 status — 창고 재기동 초기화 CIN 의 null 등.
    # 저장만 한다. 예전엔 아래 disable/empty 갈래로 떨어져 진행 주문에 keycap_cartridge_failed 가
    # 붙었고, 재기동 한 번에 36건이 쏟아져 fault 가 덮어써졌다 (flow-break-points 7번).
    if status not in stock.BLOCKING_STATUS:
        logger.warning(f"[{kind}_status] {cartridge_id} 모르는 status={status!r} — 저장만, fault 없음")
        push_process()
        return

    # disable / empty — 진행 중 주문이 그 칸을 '아직 배출 전'일 때만 fault (명세 6-3)
    #
    # disable 의 reason 이 무엇이냐에 따라 범위가 다르다.
    #   keycap_cartridge_failed : 그 칸(E_r) 하나
    #   keycap_driver_failed    : 그 선반(E/I) 전체 — 관련 글자의 모든 색이 안 나온다
    # 지금은 '출고 중에만 문제가 생긴다'고 보고, 대상 주문은 창고가 con 에 실어 보낸 order_id 다.
    # (진행 주문이 없을 때 죽는 카트리지는 기록만 하고 넘긴다 — 다음 배정에서 blockers 가 막는다.)
    reason = con.get("reason")
    shelf = stock.shelf_of(cartridge_id)
    driver_dead = (kind == "keycap" and status == "disable"
                   and reason == "keycap_driver_failed" and bool(shelf))

    if driver_dead:
        # 칸 status 는 CIN 그대로 두고 파생 상태만 따로 기록한다. 8칸에 disable 을 찍으면
        # 그중 한 칸에 idle CIN 하나만 와도 선반 고장이 조용히 풀린다.
        stock.mark_driver_failed(shelf, reason, cartridge_id, con.get("order_id"))
        logger.error(f"[keycap_status] 선반 {shelf} 드라이버 고장 ({cartridge_id}) — "
                     f"{stock.slots_of_shelf(shelf)} 차단")

    con_oid = con.get("order_id")
    current = r.get(WAREHOUSE_ORDER_KEY)
    order_id = con_oid or current
    if con_oid and current and con_oid != current:
        # 유령·지연 보고. 우리가 아는 창고 진행 주문을 기준으로 둔다.
        logger.warning(f"[{kind}_status] con.order_id={con_oid} 가 창고 진행 주문({current})과 "
                       f"다르다 — 창고 기준으로 처리")
        order_id = current

    if order_id and _still_needs(order_id, kind):
        if driver_dead:
            # 주문의 MBTI 4글자는 선반 1~4 에서 각각 하나씩 나온다. 그래서 죽은 선반이 어느
            # 것이든 그 주문이 쓰는 칸이 하나 있다 — 칸 id 비교로는 이걸 놓친다.
            mine = shelf in {stock.shelf_of(s) for s in stock.parts_of(order_id, "keycap")}
        else:
            mine = cartridge_id in stock.parts_of(order_id, kind)

        if mine:
            if status == "empty":
                set_fault(order_id, f"{kind}_empty",          # 안에서 push_process를 부른다
                          section=stock.section_of(cartridge_id))
                return
            if driver_dead:
                set_fault(order_id, "keycap_driver_failed",
                          section=stock.section_of(cartridge_id, shelf_only=True))
                return
            if kind == "keycap":
                if reason not in (None, "", "keycap_cartridge_failed"):
                    logger.warning(f"[keycap_status] 모르는 reason={reason!r} — "
                                   f"keycap_cartridge_failed 로 처리")
                set_fault(order_id, "keycap_cartridge_failed",
                          section=stock.section_of(cartridge_id))
                return
            # 보드는 카트리지가 아니라 보관대다 — 로봇팔이 트레이에 직접 넣으므로
            # 카트리지 고장이라는 게 없다 (확정본에서 board_cartridge_failed 폐기).
            logger.error(f"[board_status] {cartridge_id}={status} — 보관대 이상. fault 없음")
            push_process()
            return

    # 진행 주문과 관계없는 고장 → cnt_process 최상위 warehouse_fault (services/warehouse_fault.py).
    # empty 는 품절이라 올리지 않고(/stock/out·blockers 가 맡는다), 보드는 정의된 fault 가 없다.
    if kind == "keycap" and status == "disable":
        if driver_dead:
            warehouse_fault.add("keycap_driver_failed",
                                stock.section_of(cartridge_id, shelf_only=True))
        else:
            warehouse_fault.add("keycap_cartridge_failed", stock.section_of(cartridge_id))

    logger.warning(f"[{kind}_status] {cartridge_id}={status} — 해당 조합 주문 차단")
    push_process()                          # 진행 주문이 없어도 restock·warehouse_fault 변화는 나가야 한다
