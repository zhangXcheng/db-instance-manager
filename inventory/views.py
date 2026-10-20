"""API 视图。

约定：
  * 标准 REST 语义 + HTTP 状态码，不做 {code,msg,data} 包装
    （包装会让分页、文件下载、第三方 SDK 都变别扭）
  * 明文密码只在一个需要管理员权限的接口里出现，且强制写审计日志
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address
from django.db.models import Count, Prefetch, Sum
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from inventory.models import (
    Cluster,
    CredentialAccessLog,
    DBInstance,
    Department,
    InstanceCredential,
    InstanceStatDaily,
)
from inventory.serializers import (
    BatchProbeRequestSerializer,
    ClusterSerializer,
    CredentialAccessLogSerializer,
    DBInstanceSerializer,
    DepartmentSerializer,
    InstanceCredentialSerializer,
    InstanceStatDailySerializer,
    RebuildStatRequestSerializer,
)
from inventory.services.rotation import rotate_instance
from inventory.services.stats import build_daily_stats
from inventory.services.tcp_probe import probe_and_save, probe_many

logger = logging.getLogger("inventory.views")


def _client_ip(request) -> str | None:
    """取真实客户端 IP；反向代理场景优先 X-Forwarded-For 的第一段。"""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    raw = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
    try:
        validate_ipv46_address(raw)
        return raw
    except ValidationError:
        # 拿不到合法 IP 就留空，不能因为审计字段把整个接口打挂
        return None


class LoginView(APIView):
    """用用户名密码换 Token，方便本地/演示环境调用。

    单独限流：登录接口是暴力破解的首选目标，不设速率等于把门敞开。
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        username = request.data.get("username", "")
        password = request.data.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is None:
            return Response({"detail": "用户名或密码错误"}, status=401)
        if not user.is_active:
            return Response({"detail": "账号已停用"}, status=403)

        token, _ = Token.objects.get_or_create(user=user)
        return Response(
            {
                "token": token.key,
                "user": {
                    "id": user.pk,
                    "username": user.username,
                    "is_staff": user.is_staff,
                    "is_superuser": user.is_superuser,
                },
            }
        )


# ---------------------------------------------------------------------------
# 部门
# ---------------------------------------------------------------------------
class DepartmentViewSet(viewsets.ModelViewSet):
    queryset = (
        Department.objects.select_related("parent")
        .annotate(cluster_count=Count("clusters"))
        .order_by("id")
    )
    serializer_class = DepartmentSerializer
    filterset_fields = ["parent"]
    search_fields = ["name", "code", "owner"]
    ordering_fields = ["id", "name", "created_at"]

    @action(detail=True, methods=["get"])
    def clusters(self, request, pk=None):
        """部门下的集群列表，省一次带 filter 的额外请求。"""
        department = self.get_object()
        queryset = (
            department.clusters.annotate(instance_count=Count("instances")).order_by("id")
        )
        page = self.paginate_queryset(queryset)
        serializer = ClusterSerializer(page if page is not None else queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# 集群
# ---------------------------------------------------------------------------
class ClusterViewSet(viewsets.ModelViewSet):
    queryset = (
        Cluster.objects.select_related("department")
        .annotate(instance_count=Count("instances"))
        .order_by("id")
    )
    serializer_class = ClusterSerializer
    filterset_fields = ["department", "engine", "environment"]
    search_fields = ["name", "code", "description"]
    ordering_fields = ["id", "name", "created_at"]

    @action(detail=True, methods=["get"])
    def instances(self, request, pk=None):
        cluster = self.get_object()
        queryset = (
            cluster.instances.select_related("cluster", "cluster__department")
            # 与列表接口保持一致地预取当前凭据，否则序列化时每个实例多一次查询
            .prefetch_related(
                Prefetch(
                    "credentials",
                    queryset=InstanceCredential.objects.filter(is_current=True),
                    to_attr="current_credentials",
                )
            )
            .order_by("id")
        )
        page = self.paginate_queryset(queryset)
        # 注意不能写 `page or queryset`：空页时 [] 为假，会退回全量数据
        serializer = DBInstanceSerializer(page if page is not None else queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# 实例
# ---------------------------------------------------------------------------
class DBInstanceViewSet(viewsets.ModelViewSet):
    serializer_class = DBInstanceSerializer
    # DRF 要求 @action 传入的 initkwargs 必须是视图类已有属性；
    # 默认空串表示不限流，只有 probe 相关 action 会覆盖成 "probe"
    throttle_scope = ""
    filterset_fields = ["cluster", "status", "role", "cluster__department", "cluster__engine"]
    search_fields = ["name", "host"]
    ordering_fields = ["id", "name", "last_probe_at", "created_at"]

    def get_queryset(self):
        return (
            DBInstance.objects.select_related("cluster", "cluster__department")
            .prefetch_related(
                Prefetch(
                    "credentials",
                    queryset=InstanceCredential.objects.filter(is_current=True),
                    to_attr="current_credentials",
                )
            )
        )

    @action(
        detail=True,
        methods=["post"],
        throttle_classes=[ScopedRateThrottle],
        throttle_scope="probe",
    )
    def probe(self, request, pk=None):
        """探测单个实例的 TCP 端口是否可达，并回写状态。"""
        instance = self.get_object()
        result = probe_and_save(instance)
        return Response(
            {
                "instance_id": instance.pk,
                "name": instance.name,
                "endpoint": instance.endpoint,
                **result.as_dict(),
            }
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="probe-batch",
        throttle_classes=[ScopedRateThrottle],
        throttle_scope="probe",
    )
    def probe_batch(self, request):
        """批量探测。不传 ids 时按配置上限取前 N 个，避免一次打爆线程池。"""
        payload = BatchProbeRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ids = payload.validated_data.get("ids")

        queryset = DBInstance.objects.select_related(
            "cluster", "cluster__department"
        ).order_by("id")
        if ids is not None:
            # 显式传 [] 表示「一个都不探测」，不能因为空列表为假就退化成全量
            queryset = queryset.filter(pk__in=ids)
        else:
            queryset = queryset[: settings.TCP_PROBE_MAX_WORKERS * 10]

        instances = list(queryset)
        results = probe_many(instances)
        data = []
        for instance in instances:
            result = results.get(instance.pk)
            item = {
                "instance_id": instance.pk,
                "name": instance.name,
                "endpoint": instance.endpoint,
            }
            if result:
                item.update(result.as_dict())
            else:
                item.update({"reachable": None, "error": "未执行探测"})
            data.append(item)

        return Response({"count": len(data), "results": data})

    @action(
        detail=True,
        methods=["post"],
        url_path="rotate-password",
        permission_classes=[IsAdminUser],
    )
    def rotate_password(self, request, pk=None):
        """手动触发轮换（不等 12 小时周期），用于应急。"""
        instance = self.get_object()
        dry_run = bool(request.data.get("dry_run", False))
        outcomes = rotate_instance(instance, force=True, dry_run=dry_run)
        return Response(
            {
                "dry_run": dry_run,
                "count": len(outcomes),
                "results": [item.as_dict() for item in outcomes],
            }
        )


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------
class InstanceCredentialViewSet(viewsets.ReadOnlyModelViewSet):
    """凭据只读。新凭据只能由轮换任务产生，禁止通过 API 直接写入。"""

    queryset = InstanceCredential.objects.select_related(
        "instance", "instance__cluster"
    ).order_by("-id")
    serializer_class = InstanceCredentialSerializer
    filterset_fields = ["instance", "username", "is_current"]
    ordering_fields = ["id", "rotated_at", "version"]

    @action(detail=True, methods=["post"], permission_classes=[IsAdminUser])
    def reveal(self, request, pk=None):
        """查看明文密码。管理员限定 + 强制审计。"""
        credential = self.get_object()
        try:
            plaintext = credential.reveal_password()
        except Exception:  # noqa: BLE001 - 密钥不匹配或密文损坏
            logger.error("凭据解密失败 credential=%s", credential.pk, exc_info=True)
            return Response({"detail": "凭据解密失败，可能是密钥不匹配"}, status=409)

        CredentialAccessLog.objects.create(
            credential=credential,
            actor=request.user.username,
            action=CredentialAccessLog.Action.REVEAL,
            ip=_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:255],
        )
        return Response(
            {
                "id": credential.pk,
                "instance_id": credential.instance_id,
                "username": credential.username,
                "password": plaintext,
                "version": credential.version,
                "rotated_at": credential.rotated_at,
                "warning": "此操作已记录审计日志",
            }
        )

    @action(detail=True, methods=["get"])
    def access_logs(self, request, pk=None):
        credential = self.get_object()
        logs = credential.access_logs.all()[:100]
        return Response(CredentialAccessLogSerializer(logs, many=True).data)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
class DailyStatViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = InstanceStatDaily.objects.select_related("department", "cluster").order_by(
        "-stat_date", "id"
    )
    serializer_class = InstanceStatDailySerializer
    filterset_fields = ["stat_date", "department", "cluster"]
    ordering_fields = ["stat_date", "total_count"]

    @action(detail=False, methods=["get"], url_path="by-department")
    def by_department(self, request):
        """按部门维度汇总某一天的数据。"""
        stat_date = request.query_params.get("stat_date") or timezone.localdate().isoformat()
        rows = (
            InstanceStatDaily.objects.filter(stat_date=stat_date)
            .values("department_id", "department_name")
            .annotate(
                cluster_count=Count("cluster_id", distinct=True),
                total_count=Sum("total_count"),
                online_count=Sum("online_count"),
                offline_count=Sum("offline_count"),
            )
            .order_by("department_id")
        )
        return Response({"stat_date": stat_date, "results": list(rows)})

    @action(detail=False, methods=["post"], permission_classes=[IsAdminUser])
    def rebuild(self, request):
        """手动补跑某天的统计（幂等，重复执行结果一致）。"""
        payload = RebuildStatRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        summary = build_daily_stats(payload.validated_data.get("stat_date"))
        return Response(summary)
