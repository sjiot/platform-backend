import os
from zoneinfo import ZoneInfo

QUEUE_KEY = os.getenv("QUEUE_KEY", "order:queue")
WAREHOUSE_KEY = os.getenv("WAREHOUSE_KEY", "warehouse:occupancy")
STATION_KEY = os.getenv("STATION_KEY", "station:occupancy")
AGV_KEY = os.getenv("AGV_KEY", "agv:occupancy")
# cnt_agv 가 올린 status 원문을 그대로 보관한다 (loaded·station_arrived·unloaded·parked…).
# occupancy(idle/busy)만으로는 'AGV가 지금 어디서 뭘 하는 중인지'를 알 수 없어서
# 워치독이 정상 대기와 고장을 구분하지 못한다.
AGV_STATUS_KEY = os.getenv("AGV_STATUS_KEY", "agv:status")
AGV_COORD_KEY = os.getenv("AGV_COORD_KEY", "agv:coord")
# 대시보드 AGV 이동경로 (services/agv_trail.py).
# agv:coord 는 마지막 점(JSON), agv:trail 은 현재 구간의 점 목록, agv:leg 는 구간 번호.
# agv:coord:seq 는 점마다 오르는 순번 — 대시보드가 ?since= 로 새 점만 받아 간다.
AGV_TRAIL_KEY = os.getenv("AGV_TRAIL_KEY", "agv:trail")
AGV_SEQ_KEY = os.getenv("AGV_SEQ_KEY", "agv:coord:seq")
AGV_LEG_KEY = os.getenv("AGV_LEG_KEY", "agv:leg")
DEADLINE_KEY = os.getenv("DEADLINE_KEY", "order:deadlines")

# 영업 상태 (services/line.py). open 일 때만 POST /order 를 받는다 — 다른 API 는 막지 않는다.
# 키가 없으면 closed 로 본다. 리셋 직후·Redis 유실 뒤 아무도 확인 안 한 상태로 주문이 들어오지 않게.
LINE_STATUS_KEY = os.getenv("LINE_STATUS_KEY", "line:status")
LINE_CHANGED_AT_KEY = os.getenv("LINE_CHANGED_AT_KEY", "line:changed_at")

WAREHOUSE_ORDER_KEY = os.getenv("WAREHOUSE_ORDER_KEY", "warehouse:current_order")

# 키캡 드라이버(선반) 고장. 선반 하나가 죽으면 그 축 8칸(2글자 × 4색)을 전부 못 쓴다.
# 칸 status(warehouse:stock)는 창고가 보낸 CIN 그대로 두고, 파생 상태인 '선반 고장'만 여기 따로 둔다 —
# 8칸에 disable 을 찍어두면 그중 한 칸에 idle CIN 하나만 와도 선반 고장이 조용히 풀린다.
WAREHOUSE_DRIVER_KEY = os.getenv("WAREHOUSE_DRIVER_KEY", "warehouse:driver_failed")
# 관리자가 채웠지만 창고가 아직 반영(count>0 보고)하지 않은 칸 — cnt_process 의 *_restock 원천
WAREHOUSE_RESTOCK_KEY = os.getenv("WAREHOUSE_RESTOCK_KEY", "warehouse:restock")
STATION_ORDER_PREFIX = os.getenv("STATION_ORDER_PREFIX", "station:current_order:")
MAX_QUEUE_LEN = 3  # 조립대 개수와 동일 (그 이상 대기시켜봤자 처리 못 함)

STATION_VERIFIED_PREFIX = os.getenv("STATION_VERIFIED_PREFIX", "station:verified_at:")

# 관리자 호출. 조립대 디바이스의 호출 버튼이 누른 '지금 상태'를 담는다.
# station:occupancy 해시에 필드를 붙이지 않는 이유 — /admin/reset 이 그 해시를
# hset(mapping={s: "idle"}) 로 덮어써서 늘어난 필드를 지우지 못한다.
STATION_CALL_PREFIX = os.getenv("STATION_CALL_PREFIX", "station:call:")
STATION_TIMEOUT_SEC = int(os.getenv("STATION_TIMEOUT_SEC", "600"))  # 10분

# 노쇼(unclaim) 최소 대기 시간. 프론트가 3분 타이머를 돌리지만 그건 클라이언트 값이라
# 조작하면 도착 직후에도 unclaim을 때릴 수 있다. 서버가 arrived_at으로 다시 잰다.
# 프론트 3분(180s)보다 넉넉히 아래로 둔다 — 프론트 요청 실패 시 재시도 여유를 주기 위함.
STATION_UNCLAIM_MIN_SEC = int(os.getenv("STATION_UNCLAIM_MIN_SEC", "150"))  # 2분 30초

ORDER_COUNTER_KEY = os.getenv("ORDER_COUNTER_KEY", "order:counter")

# 영수증 용지. kiosk:paper 해시의 used = 마지막 채우기 이후 접수된 주문 수 (주문 1건 = 1매).
# 남은 용지 = PAPER_MAX_COUNT - used. POST /order 가 +1, POST /admin/paper/refill 이 0 으로.
# 대기번호 카운터(order:counter)와 따로 둔다 — 채우기로 대기번호가 1부터 다시 매겨지면 안 된다.
# 실물 재고라 TTL 도 없고 /admin/reset 도 안 지운다. 조회: GET /admin/paper
PAPER_KEY = os.getenv("PAPER_KEY", "kiosk:paper")
PAPER_MAX_COUNT = int(os.getenv("PAPER_MAX_COUNT", "900"))

# 종료 주문 상세 · 집계 (Redis ERD 5장). 90일 보관.
ARCHIVE_PREFIX = os.getenv("ARCHIVE_PREFIX", "orders:archive:")
ARCHIVE_TTL = int(os.getenv("ARCHIVE_TTL", str(90 * 24 * 3600)))

# 대시보드
EXHIBITION_START_DATE = os.getenv("EXHIBITION_START_DATE","20260918")
EXHIBITION_DAYS = int(os.getenv("EXHIBITION_DAYS", "300"))

ORDER_PAGE_BASE = os.getenv("ORDER_PAGE_BASE", "https://sjiot-backend-294910862364.asia-northeast1.run.app")

STATION_RESET_PASSWORD = os.getenv("STATION_RESET_PASSWORD")

MBTI_AXES = [("E", "I"), ("S", "N"), ("T", "F"), ("J", "P")]

# ── TTL ───────────────────────────────────────────────────────────────
# 키 성격별로 나눈다. 예전에는 TEST_KEY_TTL 하나로 '건드린 키 전부'에 같은 TTL을 걸었는데,
# 그러면 warehouse:occupancy 같은 '지금 상태' 키까지 한 시간 조용하면 사라진다.
# 상태 키(warehouse/station/agv/queue/process)에는 TTL을 걸지 않는다.
# 그것들을 지우는 경로는 POST /admin/reset 하나뿐이다.
ORDER_TTL = int(os.getenv("ORDER_TTL", str(24 * 3600)))          # 진행 중 주문 24시간
ORDER_DONE_TTL = int(os.getenv("ORDER_DONE_TTL", "600"))         # 종료된 주문 10분
ORDER_COUNTER_TTL = int(os.getenv("ORDER_COUNTER_TTL", str(90 * 24 * 3600)))   # 일련번호 90일

# 1칸의 최대 수량. /admin/reset?mode=zero 가 이 값으로 채운다.
# 키캡 카트리지와 보드 보관대는 물리적으로 용량이 달라서 따로 둔다.
KEYCAP_STOCK_MAX_COUNT = int(os.getenv("KEYCAP_STOCK_MAX_COUNT", "30"))   # 키캡 32칸
BOARD_STOCK_MAX_COUNT = int(os.getenv("BOARD_STOCK_MAX_COUNT", "20"))      # 보드 4칸

# 노쇼 폐기 마감. unclaimed 로 끝난 트레이를 AGV가 discarded 로 보고할 때까지
# 조립대를 잡아 둔다 — 그래야 폐기 명령이 cnt_process 에서 사라지지 않는다.
DISCARD_TIMEOUT_SEC = int(os.getenv("DISCARD_TIMEOUT_SEC", "300"))

COLOR_LIST = [c.strip() for c in os.getenv("COLOR_LIST", "r,y,g,b").split(",")]
BOARD_LIST = [b.strip() for b in os.getenv("BOARD_LIST", "red,yellow,green,blue").split(",")]

MP_URL = os.getenv("MP_URL")  
CB = os.getenv("CB", "Mobius")
AE_RN = os.getenv("AE_RN")   
ORIGIN = os.getenv("MOBIUS_ORIGIN")
API_KEY = os.getenv("MOBIUS_API_KEY")
LECTURE_ID = os.getenv("X-AUTH-CUSTOM-LECTURE")
CREATOR_ID = os.getenv("X-AUTH-CUSTOM-CREATOR")

STAGE_ORDER = ["queued",
               "assigned",
               "keycap_mismatched",
               "keycap_dispensed",
               "keycap_reached",
               "keycap_packed",
               "tray_reached",
               "board_mismatched",
               "board_packed",
               "pickup_reached",
               "loaded",
               "arrived",
               "unclaimed",
               "verified",
               "received",
               "completed"
               ]

STAGE_BUSY = {
    "assigned":         ["tray_robot_arm", "keycap_cartridge"],
    "keycap_mismatched":   ["keycap_cartridge"],
    "keycap_dispensed": ["keycap_conveyor"],
    "keycap_reached":   ["keycap_robot_arm"],
    "keycap_packed":    ["tray_conveyor"],
    "tray_reached":     ["board_robot_arm"],
    "board_mismatched": ["board_robot_arm"],
    "board_packed":     ["tray_conveyor"],
    "pickup_reached":   ["agv"],
    "loaded":   ["agv"],
    "arrived":   ["agv"],
    "unclaimed":   ["agv"],
    "verified":   ["agv"],
    "received":   ["agv"], # agv : parked 보고해야 다음 주문 가능
    }

SUB_CNT_KEYCAP_PARENT = os.getenv("SUB_CNT_KEYCAP_PARENT", "cnt_keycap_status")
SUB_CNT_BOARD_PARENT  = os.getenv("SUB_CNT_BOARD_PARENT",  "cnt_board_status")

SUB_CNT_KEYCAP = [c.strip() for c in os.getenv("SUB_CNT_KEYCAP", "").split(",") if c.strip()]
SUB_CNT_BOARD  = [c.strip() for c in os.getenv("SUB_CNT_BOARD",  "").split(",") if c.strip()]

# Redis warehouse:stock 의 필드명 (cnt_ 뗀 형태)
KEYCAP_SLOTS = [c.removeprefix("cnt_") for c in SUB_CNT_KEYCAP]   # E_r
BOARD_SLOTS  = [c.removeprefix("cnt_") for c in SUB_CNT_BOARD]    # r
ALL_SLOTS    = set(KEYCAP_SLOTS) | set(BOARD_SLOTS)               # 36개, 콜백 유효성 검사용

BOARD_SLOT_MAP = dict(
    p.split(":", 1) for p in os.getenv("BOARD_SLOT_MAP", "").split(",") if ":" in p
)

KST = ZoneInfo("Asia/Seoul")