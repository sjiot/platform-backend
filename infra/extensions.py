import redis
import os
import time

from redis.client import Pipeline

from infra import latency


class _TimedPipeline(Pipeline):
    def execute(self, raise_on_error=True):
        if not latency.ENABLED:
            return super().execute(raise_on_error)
        commands = list(self.command_stack)     # 실행하면 비워진다
        started = time.perf_counter()
        ok = True
        try:
            return super().execute(raise_on_error)
        except Exception:
            ok = False
            raise
        finally:
            latency.record_redis_pipeline(commands, (time.perf_counter() - started) * 1000, ok)


class _TimedRedis(redis.Redis):
    def execute_command(self, *args, **options):
        if not latency.ENABLED:
            return super().execute_command(*args, **options)
        started = time.perf_counter()
        ok = True
        try:
            return super().execute_command(*args, **options)
        except Exception:
            ok = False
            raise
        finally:
            latency.record_redis(args, (time.perf_counter() - started) * 1000, ok)

    def pipeline(self, transaction=True, shard_hint=None):
        return _TimedPipeline(self.connection_pool, self.response_callbacks, transaction, shard_hint)


REDIS_URL = os.getenv("REDIS_URL", "localhost")
r = _TimedRedis.from_url(REDIS_URL, decode_responses=True)
