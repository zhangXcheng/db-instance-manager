"""序列化器。

安全红线：任何返回体都不能出现 password_cipher，更不能出现明文密码。
凭据列表只给「账号 + 版本 + 轮换时间 + 是否有密码」。
"""
from __future__ import annotations

from rest_framework import serializers

from inventory.models import (
    Cluster,
    CredentialAccessLog,
    DBInstance,
    Department,
    InstanceCredential,
    InstanceStatDaily,
)


def _current_credential(obj: DBInstance) -> InstanceCredential | None:
    """优先用 prefetch 的结果，避免列表页 N+1。"""
    cached = getattr(obj, "current_credentials", None)
    if cached is not None:
        return cached[0] if cached else None
    return obj.credentials.filter(is_current=True).first()


# ---------------------------------------------------------------------------
# 部门
# ---------------------------------------------------------------------------
class DepartmentSerializer(serializers.ModelSerializer):
    parent_name = serializers.CharField(source="parent.name", read_only=True, default=None)
    cluster_count = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = [
            "id",
            "name",
            "code",
            "parent",
            "parent_name",
            "owner",
            "description",
            "cluster_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_cluster_count(self, obj: Department) -> int:
        if hasattr(obj, "cluster_count"):
            return obj.cluster_count
        return obj.clusters.count()

    def validate_parent(self, value: Department | None) -> Department | None:
        if value and self.instance and value.pk == self.instance.pk:
            raise serializers.ValidationError("上级部门不能是自己")
        return value


# ---------------------------------------------------------------------------
# 集群
# ---------------------------------------------------------------------------
class ClusterSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    instance_count = serializers.SerializerMethodField()

    class Meta:
        model = Cluster
        fields = [
            "id",
            "name",
            "code",
            "department",
            "department_name",
            "engine",
            "environment",
            "description",
            "instance_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_instance_count(self, obj: Cluster) -> int:
        if hasattr(obj, "instance_count"):
            return obj.instance_count
        return obj.instances.count()


# ---------------------------------------------------------------------------
# 实例
# ---------------------------------------------------------------------------
class DBInstanceSerializer(serializers.ModelSerializer):
    cluster_name = serializers.CharField(source="cluster.name", read_only=True)
    department_id = serializers.IntegerField(source="cluster.department_id", read_only=True)
    department_name = serializers.CharField(source="cluster.department.name", read_only=True)
    endpoint = serializers.CharField(read_only=True)
    current_credential = serializers.SerializerMethodField()

    class Meta:
        model = DBInstance
        fields = [
            "id",
            "name",
            "cluster",
            "cluster_name",
            "department_id",
            "department_name",
            "host",
            "port",
            "role",
            "status",
            "version",
            "endpoint",
            "last_probe_at",
            "last_probe_ok",
            "last_probe_latency_ms",
            "last_probe_error",
            "current_credential",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "last_probe_at",
            "last_probe_ok",
            "last_probe_latency_ms",
            "last_probe_error",
            "created_at",
            "updated_at",
        ]

    def get_current_credential(self, obj: DBInstance) -> dict | None:
        credential = _current_credential(obj)
        if credential is None:
            return None
        return {
            "id": credential.pk,
            "username": credential.username,
            "version": credential.version,
            "rotated_at": credential.rotated_at,
            "last_error": credential.last_error,
        }

    def validate_host(self, value: str) -> str:
        host = (value or "").strip()
        if not host:
            raise serializers.ValidationError("主机地址不能为空")
        # 常见误填：把 jdbc/redis 连接串整段贴进来
        for prefix in ("http://", "https://", "jdbc:", "redis://"):
            if host.startswith(prefix):
                raise serializers.ValidationError("请填写主机名或 IP，不要带协议前缀")
        return host


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------
class InstanceCredentialSerializer(serializers.ModelSerializer):
    instance_name = serializers.CharField(source="instance.name", read_only=True)
    endpoint = serializers.CharField(source="instance.endpoint", read_only=True)
    masked_password = serializers.CharField(read_only=True)

    class Meta:
        model = InstanceCredential
        fields = [
            "id",
            "instance",
            "instance_name",
            "endpoint",
            "username",
            "masked_password",
            "version",
            "is_current",
            "rotated_at",
            "last_attempt_at",
            "last_error",
            "created_at",
        ]
        read_only_fields = fields


class CredentialAccessLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = CredentialAccessLog
        fields = ["id", "credential", "actor", "action", "ip", "user_agent", "created_at"]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
class InstanceStatDailySerializer(serializers.ModelSerializer):
    class Meta:
        model = InstanceStatDaily
        fields = [
            "id",
            "stat_date",
            "department",
            "department_name",
            "cluster",
            "cluster_name",
            "total_count",
            "online_count",
            "offline_count",
            "unknown_count",
            "updated_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------
class BatchProbeRequestSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        max_length=100,
        help_text="要探测的实例 ID 列表，留空表示全部（最多 100 个）",
    )


class RebuildStatRequestSerializer(serializers.Serializer):
    stat_date = serializers.DateField(required=False, help_text="补跑指定日期，格式 YYYY-MM-DD")


# ---------------------------------------------------------------------------
# 以下仅用于 OpenAPI schema（drf-spectacular），不参与实际读写
# ---------------------------------------------------------------------------
class LoginRequestSerializer(serializers.Serializer):
    username = serializers.CharField(help_text="Django 用户名")
    password = serializers.CharField(style={"input_type": "password"})


class LoginUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    is_staff = serializers.BooleanField()
    is_superuser = serializers.BooleanField()


class LoginResponseSerializer(serializers.Serializer):
    token = serializers.CharField()
    user = LoginUserSerializer()


class ProbeResultSerializer(serializers.Serializer):
    instance_id = serializers.IntegerField()
    name = serializers.CharField()
    endpoint = serializers.CharField(help_text="host:port")
    reachable = serializers.BooleanField()
    latency_ms = serializers.FloatField(allow_null=True, help_text="TCP 握手耗时")
    error = serializers.CharField(allow_null=True, required=False)
    checked_at = serializers.DateTimeField()


class BatchProbeResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    results = ProbeResultSerializer(many=True)


class RotationOutcomeSerializer(serializers.Serializer):
    instance_id = serializers.IntegerField()
    instance_name = serializers.CharField()
    username = serializers.CharField()
    status = serializers.CharField(help_text="rotated / skipped / failed / dry_run")
    version = serializers.IntegerField(allow_null=True)
    error = serializers.CharField(allow_null=True, required=False)


class RotateResponseSerializer(serializers.Serializer):
    dry_run = serializers.BooleanField()
    count = serializers.IntegerField()
    results = RotationOutcomeSerializer(many=True)


class RevealResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    instance_id = serializers.IntegerField()
    username = serializers.CharField()
    password = serializers.CharField(help_text="明文密码，本次调用会被写入审计日志")
    version = serializers.IntegerField()
    rotated_at = serializers.DateTimeField()
    warning = serializers.CharField()


class DepartmentStatSerializer(serializers.Serializer):
    department_id = serializers.IntegerField()
    department_name = serializers.CharField()
    cluster_count = serializers.IntegerField()
    total_count = serializers.IntegerField()
    online_count = serializers.IntegerField()
    offline_count = serializers.IntegerField()


class DepartmentStatResponseSerializer(serializers.Serializer):
    stat_date = serializers.DateField()
    results = DepartmentStatSerializer(many=True)
