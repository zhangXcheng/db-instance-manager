"""TCP 端口连通性探测。

刻意没有用 `telnetlib` / 第三方库：
  * telnetlib 在 Python 3.13 已被移除
  * 探测只需要「三次握手是否完成」，socket.create_connection 足够且最轻

注意点：
  * DNS 解析耗时不受 socket timeout 约束，域名异常时可能明显超出预期。
    生产环境建议做 DNS 缓存或提前解析，这里用整体超时兜底并明确提示。
  * 探测是 IO 密集型，批量场景用线程池并发，避免 N × timeout 的串行雪崩。
"""
from __future__ import annotations

import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter

from django.conf import settings
from django.utils import timezone

from inventory.models import DBInstance

logger = logging.getLogger("inventory.probe")


@dataclass(frozen=True)
class ProbeResult:
    reachable: bool
    latency_ms: float | None
    error: str | None
    checked_at: datetime

    def as_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "checked_at": self.checked_at.isoformat(),
        }


def probe_tcp(host: str, port: int, timeout: float | None = None) -> ProbeResult:
    """尝试与 host:port 建立 TCP 连接，返回是否可达及握手耗时。"""
    timeout = settings.TCP_PROBE_TIMEOUT if timeout is None else timeout
    started = perf_counter()

    try:
        # create_connection 内部会处理 IPv4/IPv6 与 DNS 解析
        with socket.create_connection((host, int(port)), timeout=timeout):
            latency = (perf_counter() - started) * 1000
            return ProbeResult(True, round(latency, 2), None, timezone.now())
    except TimeoutError:
        # ConnectionRefusedError / TimeoutError 都是 OSError 子类，必须先捕获更具体的
        return ProbeResult(False, None, f"连接超时（{timeout}s）", timezone.now())
    except ConnectionRefusedError:
        return ProbeResult(False, None, "连接被拒绝（端口未监听）", timezone.now())
    except socket.gaierror as exc:
        return ProbeResult(False, None, f"域名解析失败：{exc.strerror or exc}", timezone.now())
    except OSError as exc:
        return ProbeResult(False, None, f"网络错误：{exc.strerror or exc}", timezone.now())


def probe_and_save(instance: DBInstance, timeout: float | None = None) -> ProbeResult:
    """探测并把结果回写到实例上。

    用 update_fields 精确更新，避免整行写带来的写放大与并发覆盖。
    """
    result = probe_tcp(instance.host, instance.port, timeout=timeout)

    instance.status = DBInstance.Status.ONLINE if result.reachable else DBInstance.Status.OFFLINE
    instance.last_probe_at = result.checked_at
    instance.last_probe_ok = result.reachable
    instance.last_probe_latency_ms = result.latency_ms
    instance.last_probe_error = (result.error or "")[:255]
    instance.save(
        update_fields=[
            "status",
            "last_probe_at",
            "last_probe_ok",
            "last_probe_latency_ms",
            "last_probe_error",
            "updated_at",
        ]
    )
    logger.info(
        "probe instance=%s endpoint=%s reachable=%s latency_ms=%s",
        instance.pk,
        instance.endpoint,
        result.reachable,
        result.latency_ms,
    )
    return result


def probe_many(instances: list[DBInstance], max_workers: int | None = None) -> dict[int, ProbeResult]:
    """并发探测多个实例。

    socket 操作放在工作线程，DB 写入统一回到调用线程，
    避免在子线程里各自建立数据库连接（Django 的 ORM 连接是线程绑定的）。
    """
    max_workers = max_workers or settings.TCP_PROBE_MAX_WORKERS
    results: dict[int, ProbeResult] = {}
    if not instances:
        return results

    with ThreadPoolExecutor(max_workers=min(max_workers, len(instances))) as pool:
        future_map = {
            pool.submit(probe_tcp, item.host, item.port): item for item in instances
        }
        raw = {future_map[f]: f.result() for f in future_map}

    for instance, result in raw.items():
        instance.status = (
            DBInstance.Status.ONLINE if result.reachable else DBInstance.Status.OFFLINE
        )
        instance.last_probe_at = result.checked_at
        instance.last_probe_ok = result.reachable
        instance.last_probe_latency_ms = result.latency_ms
        instance.last_probe_error = (result.error or "")[:255]
        results[instance.pk] = result

    if raw:
        DBInstance.objects.bulk_update(
            list(raw.keys()),
            [
                "status",
                "last_probe_at",
                "last_probe_ok",
                "last_probe_latency_ms",
                "last_probe_error",
                "updated_at",
            ],
        )
    return results
