"""측정 환경을 JSON 으로 남긴다. 실험을 시작할 때 1번 돌린다. 읽기만 하고 비밀값은 적지 않는다.

  python -m infra.latency_env --run-id before-r1
  python -m infra.latency_env --gunicorn "-w 1"         실행 설정을 함께 적는다

gunicorn 설정은 코드에서 알 수 없으므로 직접 넣는다.
결과: logs/latency/env-{run-id}.json
"""
import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import statistics
import subprocess
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from config import KST                  # noqa: E402
from infra.latency import LOG_DIR       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(*command):
    """명령 출력. 없는 명령이거나 실패하면 None — 환경 기록이 측정을 막으면 안 된다."""
    if not shutil.which(command[0]):
        return None
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=10, cwd=ROOT,
                              check=True).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def read_file(path):
    try:
        with open(path, encoding="utf-8") as fp:
            return fp.read()
    except OSError:
        return None


def os_release():
    fields = dict(line.split("=", 1) for line in (read_file("/etc/os-release") or "").splitlines() if "=" in line)
    return fields.get("PRETTY_NAME", "").strip('"') or None


def mem_total_kb():
    for line in (read_file("/proc/meminfo") or "").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1])
    return None


def redis_info(samples=200):
    """Redis 버전과 PING 왕복 시간. 주소는 적지 않는다 — 비밀번호가 들어 있을 수 있다."""
    import redis
    try:
        client = redis.from_url(os.getenv("REDIS_URL", "localhost"), decode_responses=True)
        version = client.info("server").get("redis_version")
        values = []
        for _ in range(samples):
            started = time.perf_counter()
            client.ping()
            values.append((time.perf_counter() - started) * 1000)
    except redis.RedisError as e:
        return {"error": type(e).__name__}
    values.sort()
    return {"version": version, "ping_samples": samples,
            "ping_p50_ms": round(statistics.median(values), 3),
            "ping_p95_ms": round(values[int(len(values) * 0.95)], 3)}


def collect(gunicorn):
    return {
        "at": datetime.now(KST).isoformat(timespec="seconds"),
        "commit": run("git", "rev-parse", "--short", "HEAD"),
        "dirty": bool(run("git", "status", "--porcelain")),
        "os": os_release() or platform.platform(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count(),
        "load_avg": os.getloadavg(),
        "mem_total_kb": mem_total_kb(),
        "python": platform.python_version(),
        "gunicorn_version": importlib.metadata.version("gunicorn"),
        "gunicorn_args": gunicorn,
        "redis": redis_info(),
        "chrony": run("chronyc", "tracking"),
        "default_route": run("ip", "route", "show", "default"),
    }


def main():
    ap = argparse.ArgumentParser(description="측정 환경 기록")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--gunicorn", default=None, help='gunicorn 실행 옵션. 예: "-w 1 --threads 1"')
    ap.add_argument("--dir", default=LOG_DIR)
    args = ap.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    path = os.path.join(args.dir, f"env-{args.run_id}.json")
    info = {"run_id": args.run_id, **collect(args.gunicorn)}
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(info, fp, ensure_ascii=False, indent=2)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print(f"저장: {path}")


if __name__ == "__main__":
    main()
