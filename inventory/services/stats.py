"""每日实例数量统计。

两条硬性要求：
  1. **幂等**：任务可能被重复触发（beat 抖动、人工补跑），
     同一天同一集群只能有一行，重跑必须是覆盖而不是追加。
  2. **完整**：即使某个集群当天没有实例，也要写一条 0 值记录，
     否则报表按日期聚合时会出现「这个集群今天消失了」的假象。

实现上用一次 GROUP BY 拿到全部计数，再与集群全量表左连接补齐，
避免 N+1；写入用 update_or_create 保证幂等。
"""
from __future__ import annotations

import logging
from datetime import date

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from inventory.models import Cluster, DBInstance, InstanceStatDaily

logger = logging.getLogger("inventory.stats")


def build_daily_stats(stat_date: date | None = None) -> dict:
    """生成（或覆盖）指定日期的统计快照，返回汇总信息。"""
    stat_date = stat_date or timezone.localdate()

    # 一次 GROUP BY 拿到「集群 × 状态」的计数
    aggregated = (
        DBInstance.objects.values("cluster_id")
        .annotate(
            total=Count("id"),
            online=Count("id", filter=Q(status=DBInstance.Status.ONLINE)),
            offline=Count("id", filter=Q(status=DBInstance.Status.OFFLINE)),
        )
    )
    by_cluster = {row["cluster_id"]: row for row in aggregated}

    clusters = list(Cluster.objects.select_related("department").all())

    rows = []
    for cluster in clusters:
        counts = by_cluster.get(cluster.pk, {"total": 0, "online": 0, "offline": 0})
        total = counts["total"]
        online = counts["online"]
        offline = counts["offline"]
        rows.append(
            InstanceStatDaily(
                stat_date=stat_date,
                department=cluster.department,
                cluster=cluster,
                # 存快照，避免日后改名导致历史报表失真
                department_name=cluster.department.name,
                cluster_name=cluster.name,
                total_count=total,
                online_count=online,
                offline_count=offline,
                # 用减法而不是再数一次：将来新增状态时不会漏计
                unknown_count=max(total - online - offline, 0),
            )
        )

    written = 0
    with transaction.atomic():
        for row in rows:
            InstanceStatDaily.objects.update_or_create(
                stat_date=stat_date,
                cluster=row.cluster,
                defaults={
                    "department": row.department,
                    "department_name": row.department_name,
                    "cluster_name": row.cluster_name,
                    "total_count": row.total_count,
                    "online_count": row.online_count,
                    "offline_count": row.offline_count,
                    "unknown_count": row.unknown_count,
                },
            )
            written += 1

    summary = {
        "stat_date": stat_date.isoformat(),
        "cluster_rows": written,
        "total_instances": sum(r.total_count for r in rows),
        "online_instances": sum(r.online_count for r in rows),
    }
    logger.info("每日统计完成 %s", summary)
    return summary


def parse_stat_date(raw: str | None) -> date | None:
    """把 '2026-10-20' 解析成 date；非法格式抛 ValueError。

    HTTP 入口已由 serializer 的 DateField 先校验，这里主要服务 Celery 任务参数。
    """
    if not raw:
        return None
    return date.fromisoformat(raw)
