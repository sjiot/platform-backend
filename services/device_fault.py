"""주문에 붙일 수 없는 설비 고장 목록 — cnt_process 최상위 agv_fault · warehouse_fault 공용.

항목 형식은 주문 fault 와 같은 두 축에 시각만 더한다: {fault, fault_section, at}.
목록이 비면 cnt_process 에 null 로 나간다 — 주문 fault 와 같이 'null = 해결'로 읽는다.

Redis Hash 하나에 담고 필드 이름은 설비마다 고른다.
  AGV  : fault 이름 (AGV 는 한 대라 같은 고장이 두 줄일 일이 없다)
  창고 : fault_section (같은 fault 가 여러 칸에서 날 수 있다. 선반 고장 8칸은 한 줄로 합쳐진다)
"""
import json
from datetime import datetime

from config import KST
from infra.extensions import r
from infra.logger import logger


class DeviceFaults:
    def __init__(self, key, tag):
        self.key = key
        self.tag = tag

    def add(self, fault, section, field):
        """같은 field 가 다시 오면 덮어쓴다 (재전송·반복 보고)."""
        item = {"fault": fault, "fault_section": section,
                "at": datetime.now(KST).isoformat(timespec="seconds")}
        r.hset(self.key, field, json.dumps(item, ensure_ascii=False))
        logger.error(f"[{self.tag}] 주문 없는 고장 <- {fault} @{section}")

    def items(self):
        """cnt_process 에 실을 값. 없으면 None. field 순 — 순서가 흔들리면 push 지문이 달라진다."""
        raw = r.hgetall(self.key)
        if not raw:
            return None
        out = []
        for field in sorted(raw):
            try:
                out.append(json.loads(raw[field]))
            except (TypeError, ValueError):
                out.append({"fault": None, "fault_section": field, "at": None})
        return out

    def active(self):
        return bool(r.exists(self.key))

    def remove(self, fields):
        """그 field 들만 지운다. 실제로 지운 것을 돌려준다. push 는 호출부가 한다."""
        fields = [f for f in fields if f]
        if not fields:
            return []
        present = [f for f in fields if r.hexists(self.key, f)]
        if present:
            r.hdel(self.key, *present)
            logger.info(f"[{self.tag}] 고장 해제 {present}")
        return present

    def clear(self):
        """전부 지우고 지운 field 목록을 돌려준다. push 는 호출부가 한다."""
        fields = sorted(r.hkeys(self.key))
        r.delete(self.key)
        return fields
