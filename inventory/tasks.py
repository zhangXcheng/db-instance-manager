"""Celery 定时任务。

两个任务都由 beat 调度：
  * rotate_all_passwords：每 12 小时，轮换所有到期凭据
  * aggregate_daily_instance_stats：每天 00:00，统计实例数量

任务本身只做「编排 + 汇总 + 日志」，业务逻辑放在 services 里，
这样单测可以直接调 service，不依赖 broker。
"""
from __future__ import annotations

import logging
from collections import Counter

from celery import shared_task
from django.utils import timezone

from inventory.services.rotation import FAILED, rotate_due_credentials
from inventory.services.stats import build_daily_stats, parse_stat_date

logger = logging.getLogger("inventory.tasks")


@shared_task(name="inventory.tasks.rotate_all_passwords", bind=True, max_retries=0)
def rotate_all_passwords(self, force: bool = False, dry_run: bool = False, limit: int | None = None) -> dict:
    """轮换到期密码。

    max_retries=0：这里不做自动重试。轮换失败往往意味着目标库不可达，
    立刻重试大概率还是失败，反而刷屏；交给下一个 12 小时周期即可，
    失败信息已落在 instance_credential.last_error 上可供排查。
    """
    started = timezone.now()
    outcomes = rotate_due_credentials(force=force, dry_run=dry_run, limit=limit)
    counter = Counter(o.status for o in outcomes)

    failures = [o.as_dict() for o in outcomes if o.status == FAILED]
    result = {
        "started_at": started.isoformat(),
        "duration_ms": round((timezone.now() - started).total_seconds() * 1000, 2),
        "total": len(outcomes),
        "summary": dict(counter),
        "failures": failures[:50],  # 结果里不要塞全量，避免 payload 过大
    }

    if failures:
        logger.error("密码轮换存在失败项 count=%s", len(failures))
    logger.info("密码轮换任务结束 %s", result["summary"])
    return result


@shared_task(name="inventory.tasks.aggregate_daily_instance_stats", bind=True, max_retries=0)
def aggregate_daily_instance_stats(self, stat_date: str | None = None) -> dict:
    """按「部门 × 集群」统计实例数量并落库。

    stat_date 传字符串（如 '2026-10-20'）以支持补跑历史；不传则取当天。
    """
    parsed = parse_stat_date(stat_date)
    return build_daily_stats(parsed)
