"""측정 기록 집계. 서버와 별개 프로세스로 파일만 읽는다.

  python -m infra.latency_report                        오늘 파트별 집계
  python -m infra.latency_report --by-target            cnt·Redis 키별 집계 추가
  python -m infra.latency_report --by-worker            워커(pid)·스레드별 집계 추가
  python -m infra.latency_report --by-request           요청 종류별 Redis 왕복·CIN 수 추가
  python -m infra.latency_report --stages               주문 단계 사이 소요 시간 추가
  python -m infra.latency_report --date 2026-11-04      특정 날짜
  python -m infra.latency_report --follow --window 60   최근 60초 실시간 갱신
  python -m infra.latency_report --json out.json        JSON 배열로 변환
"""
import argparse
import json
import os
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from config import KST                  # noqa: E402
from infra.latency import LOG_DIR       # noqa: E402

PARTS = ("redis_write", "redis_read", "mobius_write", "mobius_read",
         "mobius_subscribe", "callback_handle", "http_request", "watchdog_expire")
EVENT_PARTS = ("order_stage", "process_push_skipped")   # 시각·횟수만 보는 사건. ms 집계에서 뺀다
REQUEST_PARTS = ("callback_handle", "http_request", "watchdog_expire")


def percentile(ordered, q):
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def summarize(entries, key):
    groups = defaultdict(list)
    for entry in entries:
        if entry["part"] not in EVENT_PARTS:
            groups[key(entry)].append(entry)

    rows = []
    for name, items in groups.items():
        values = sorted(e["ms"] for e in items)
        rows.append({
            "name": name,
            "count": len(values),
            "failed": sum(1 for e in items if not e.get("ok", True)),
            "p50": statistics.median(values),
            "p95": percentile(values, 0.95),
            "max": values[-1],
            "total": sum(values),
        })
    return rows


def part_rows(entries):
    order = {p: i for i, p in enumerate(PARTS)}
    return sorted(summarize(entries, lambda e: e["part"]),
                  key=lambda row: order.get(row["name"], len(order)))


def target_of(entry):
    note = entry.get("note") or {}
    return f'{entry["part"]} {note.get("cnt") or note.get("key") or note.get("command") or "-"}'


def target_rows(entries, top):
    return sorted(summarize(entries, target_of), key=lambda row: -row["total"])[:top]


def worker_rows(entries):
    """part 별로 나눠 합산이 겹치지 않게 한다 — callback_handle 은 Redis·Mobius 시간을 포함한다."""
    return sorted(summarize(entries, lambda e: f'pid={e.get("pid")} {e.get("thread")} {e["part"]}'),
                  key=lambda row: row["name"])


def request_name(cnt):
    """재고 컨테이너 36개처럼 칸마다 다른 경로를 하나로 묶는다."""
    parts = cnt.split("/")
    return "/".join(parts[:2]) + "/*" if len(parts) > 2 else cnt


def request_rows(entries):
    """trace 1건 = 요청 1건. 요청 종류별로 Redis 왕복·CIN 수와 시간 구성을 낸다."""
    traces = defaultdict(list)
    for entry in entries:
        if entry.get("trace_id"):
            traces[entry["trace_id"]].append(entry)

    groups = defaultdict(list)
    for items in traces.values():
        head = next((e for e in items if e["part"] in REQUEST_PARTS), None)
        if head is None:
            continue                            # 집계 범위 경계에서 잘린 요청
        note = head.get("note") or {}
        redis = [e for e in items if e["part"] in ("redis_read", "redis_write")]
        writes = [e for e in items if e["part"] == "mobius_write"]
        mobius = [e for e in items if e["part"] in ("mobius_read", "mobius_write")]
        name = request_name(note["cnt"]) if note.get("cnt") else note.get("route") or head["part"]
        groups[name].append({
            "redis_calls": len(redis),
            "cin": len(writes),
            "redis_ms": sum(e["ms"] for e in redis),
            "mobius_ms": sum(e["ms"] for e in mobius),
            "total_ms": head["ms"],
        })

    rows = []
    for name, items in groups.items():
        rows.append({
            "name": name,
            "count": len(items),
            "redis_calls": statistics.median(i["redis_calls"] for i in items),
            "redis_calls_max": max(i["redis_calls"] for i in items),
            "cin": statistics.median(i["cin"] for i in items),
            "cin_max": max(i["cin"] for i in items),
            "redis_ms": statistics.median(i["redis_ms"] for i in items),
            "mobius_ms": statistics.median(i["mobius_ms"] for i in items),
            "total_p50": statistics.median(i["total_ms"] for i in items),
            "total_p95": percentile(sorted(i["total_ms"] for i in items), 0.95),
        })
    return sorted(rows, key=lambda row: -row["count"] * row["total_p50"])


def render_requests(rows):
    width = max([len(row["name"]) for row in rows] + [6])
    lines = [f'{"요청":<{width - 2}} {"건수":>6} {"Redis수":>9} {"최대":>5} {"CIN":>5} {"최대":>5} '
             f'{"Redis ms":>9} {"Mobius ms":>10} {"p50":>9} {"p95":>9}']
    for row in rows:
        lines.append(f'{row["name"]:<{width}} {row["count"]:>8} {row["redis_calls"]:>10.1f} '
                     f'{row["redis_calls_max"]:>7} {row["cin"]:>5.1f} {row["cin_max"]:>7} '
                     f'{row["redis_ms"]:>9.2f} {row["mobius_ms"]:>10.2f} '
                     f'{row["total_p50"]:>9.2f} {row["total_p95"]:>9.2f}')
    return "\n".join(lines)


def stage_rows(entries):
    """주문별로 단계 진입 시각의 차이를 구해 '앞 단계 -> 뒤 단계' 별로 묶는다. 단위 초."""
    orders = defaultdict(list)
    for entry in entries:
        if entry["part"] == "order_stage":
            orders[entry["note"]["order_id"]].append(entry)

    groups = defaultdict(list)
    for items in orders.values():
        items.sort(key=lambda e: e["created_at"])
        for before, after in zip(items, items[1:]):
            seconds = (datetime.fromisoformat(after["created_at"])
                       - datetime.fromisoformat(before["created_at"])).total_seconds()
            groups[f'{before["note"]["stage"]} -> {after["note"]["stage"]}'].append(seconds)

    return [{"name": name, "count": len(values), "failed": 0,
             "p50": statistics.median(values), "p95": percentile(sorted(values), 0.95),
             "max": max(values), "total": sum(values)}
            for name, values in sorted(groups.items(), key=lambda kv: -sum(kv[1]))]


def event_counts(entries):
    return {part: sum(1 for e in entries if e["part"] == part) for part in EVENT_PARTS}


def render(rows):
    width = max([len(row["name"]) for row in rows] + [6])
    lines = [f'{"대상":<{width - 2}} {"건수":>6} {"실패":>4} {"p50":>9} {"p95":>9} {"max":>9} {"합계":>10}']
    for row in rows:
        lines.append(f'{row["name"]:<{width}} {row["count"]:>8} {row["failed"]:>6} '
                     f'{row["p50"]:>9.2f} {row["p95"]:>9.2f} {row["max"]:>9.2f} {row["total"]:>12.1f}')
    return "\n".join(lines)


def parse_line(line):
    """쓰는 중인 마지막 줄이나 깨진 줄은 건너뛴다."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) and "part" in entry and "ms" in entry else None


def load(path, run_id=None):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fp:
        entries = [e for e in map(parse_line, fp) if e]
    return [e for e in entries if not run_id or e.get("run_id") == run_id]


def today():
    return datetime.now(KST).strftime("%Y-%m-%d")


def log_path(log_dir, date):
    return os.path.join(log_dir, f"{date}.jsonl")


def follow(args):
    entries, path, offset, partial = [], None, 0, b""
    while True:
        current = log_path(args.dir, today())
        if current != path:                     # 자정에 새 파일로 넘어간다
            path, offset, partial = current, 0, b""
        if os.path.exists(path):
            with open(path, "rb") as fp:
                fp.seek(offset)
                chunk = fp.read()
                offset = fp.tell()
            lines = (partial + chunk).split(b"\n")
            partial = lines.pop()
            for raw in lines:
                entry = parse_line(raw.decode("utf-8", "replace"))
                if entry and (not args.run_id or entry.get("run_id") == args.run_id):
                    entries.append(entry)

        cutoff = datetime.now(KST) - timedelta(seconds=args.window)
        entries = [e for e in entries if datetime.fromisoformat(e["created_at"]) >= cutoff]

        print("\033[2J\033[H", end="")
        print(f'{datetime.now(KST).strftime("%H:%M:%S")}  최근 {args.window}초  {len(entries)}건  단위 ms  (Ctrl+C 종료)\n')
        if entries:
            print(render(part_rows(entries)))
            if args.by_target:
                print("\n" + render(target_rows(entries, args.top)))
            if args.by_worker:
                print("\n" + render(worker_rows(entries)))
        else:
            print(f"기록 없음: {path}\n서버를 LATENCY_LOG=1 로 실행했는지 확인한다")
        time.sleep(args.interval)


def main():
    ap = argparse.ArgumentParser(description="네트워크 구간 측정 기록 집계")
    ap.add_argument("--dir", default=LOG_DIR)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD, 기본은 오늘")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--by-target", action="store_true")
    ap.add_argument("--by-worker", action="store_true")
    ap.add_argument("--by-request", action="store_true")
    ap.add_argument("--stages", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--interval", type=float, default=2)
    ap.add_argument("--json", default=None, help="기록을 JSON 배열 파일로 저장")
    args = ap.parse_args()

    if args.follow:
        try:
            follow(args)
        except KeyboardInterrupt:
            return
        return

    path = log_path(args.dir, args.date or today())
    entries = load(path, args.run_id)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fp:
            json.dump(entries, fp, ensure_ascii=False, indent=2)
        print(f"{len(entries)}건 저장: {args.json}")
        return
    if not entries:
        print(f"기록 없음: {path}")
        return
    print(f"{path}  {len(entries)}건  단위 ms\n")
    print(render(part_rows(entries)))
    if args.by_target:
        print("\n" + render(target_rows(entries, args.top)))
    if args.by_worker:
        print("\n" + render(worker_rows(entries)))
    if args.by_request:
        rows = request_rows(entries)
        print("\n요청별 중앙값\n" + (render_requests(rows) if rows else "trace 가 붙은 요청 없음"))
    if args.stages:
        rows = stage_rows(entries)
        print("\n단계 사이 소요 시간 (단위 초)\n" + (render(rows) if rows else "단계 기록 없음"))
    counts = event_counts(entries)
    print(f'\n단계 전이 {counts["order_stage"]}건, cnt_process 중복 생략 {counts["process_push_skipped"]}건')


if __name__ == "__main__":
    main()
