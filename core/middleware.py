"""请求耗时中间件。

三个容易踩的坑，这里都处理了：
  1. 必须用 time.perf_counter()（单调时钟），time.time() 会被 NTP 校时影响，
     出现「负耗时」这种诡异数据。
  2. 视图抛异常时 get_response 会向上冒泡，若不 try/finally 就统计不到失败请求，
     而失败请求恰恰是最需要看耗时的。
  3. 流式响应（StreamingHttpResponse / FileResponse）返回时只代表「首字节」就绪，
     真实传输耗时在迭代阶段，不能简单当作总耗时。

除了写响应头，还输出结构化日志，便于采集到 ELK / Loki 后算 P95、P99。
"""
from __future__ import annotations

import logging
import time

from django.conf import settings

logger = logging.getLogger("core.timing")


class RequestTimingMiddleware:
    HEADER = "X-Request-Duration-ms"

    # 健康检查、静态资源不统计，避免污染指标
    EXEMPT_PREFIXES = ("/healthz", "/static/", "/media/", "/favicon.ico")

    def __init__(self, get_response):
        self.get_response = get_response
        self.slow_threshold_ms = getattr(settings, "SLOW_REQUEST_THRESHOLD_MS", 500)

    def __call__(self, request):
        if request.path.startswith(self.EXEMPT_PREFIXES):
            return self.get_response(request)

        started = time.perf_counter()
        try:
            response = self.get_response(request)
        except Exception:
            # 只有「比本中间件更外层」的异常才会冒泡到这里。
            # 视图异常通常已被 Django 的 convert_exception_to_response 转成 500 响应，
            # 走不到这个分支——所以下面还要按状态码兜底判定。
            self._report(request, (time.perf_counter() - started) * 1000, status=500, failed=True)
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000
        streaming = getattr(response, "streaming", False)
        self._report(
            request,
            elapsed_ms,
            status=response.status_code,
            # 5xx 说明服务端出错，同样需要按「失败请求」告警
            failed=response.status_code >= 500,
            streaming=streaming,
        )

        response[self.HEADER] = f"{elapsed_ms:.2f}"
        return response

    def _report(
        self,
        request,
        elapsed_ms: float,
        status: int,
        failed: bool = False,
        streaming: bool = False,
    ) -> None:
        user = getattr(request, "user", None)
        user_repr = getattr(user, "id", None) if getattr(user, "is_authenticated", False) else "-"
        extra = {
            "method": request.method,
            "path": request.path,
            "status": status,
            "duration_ms": round(elapsed_ms, 2),
            "user_id": user_repr,
            "streaming": streaming,
        }
        # 用 extra 挂结构化字段，日志采集端可直接提取
        if failed:
            logger.warning("request failed duration=%s", extra, extra=extra)
        elif elapsed_ms >= self.slow_threshold_ms:
            logger.warning("slow request duration_ms=%.2f", elapsed_ms, extra=extra)
        else:
            logger.info("request duration_ms=%.2f", elapsed_ms, extra=extra)
