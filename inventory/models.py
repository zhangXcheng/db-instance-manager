"""领域模型。

关系：Department 1—N Cluster 1—N DBInstance 1—N InstanceCredential

几个刻意的设计选择：
  * 凭据单独建表且带 version，而不是在实例表上放两列 password/username。
    因为「轮换」本质是产生新版本、旧版本作废，历史版本必须留痕以便回滚与审计。
  * 密码只存密文（password_cipher），明文永不落库、永不进日志、默认不出接口。
  * 统计表冗余存部门/集群名称快照，避免维表改名后历史报表跟着"变脸"。
"""
from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from core.crypto import get_cipher


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class Department(TimeStampedModel):
    """部门。支持父子层级，便于按组织树汇总。"""

    name = models.CharField("部门名称", max_length=64, unique=True)
    code = models.CharField("部门编码", max_length=32, unique=True)
    parent = models.ForeignKey(
        "self",
        verbose_name="上级部门",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
    )
    owner = models.CharField("负责人", max_length=32, blank=True, default="")
    description = models.TextField("备注", blank=True, default="")

    class Meta:
        db_table = "department"
        verbose_name = "部门"
        verbose_name_plural = "部门"
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.name}({self.code})"


class Cluster(TimeStampedModel):
    """集群：一组提供相同服务、可互相主从切换的实例。"""

    class Engine(models.TextChoices):
        MYSQL = "mysql", "MySQL"
        POSTGRESQL = "postgresql", "PostgreSQL"
        REDIS = "redis", "Redis"
        MONGODB = "mongodb", "MongoDB"
        SQLSERVER = "sqlserver", "SQL Server"

    class Environment(models.TextChoices):
        PROD = "prod", "生产"
        STAGING = "staging", "预发"
        DEV = "dev", "开发"
        TEST = "test", "测试"

    name = models.CharField("集群名称", max_length=64)
    code = models.CharField("集群编码", max_length=64, unique=True)
    department = models.ForeignKey(
        Department, verbose_name="所属部门", on_delete=models.CASCADE, related_name="clusters"
    )
    engine = models.CharField("数据库类型", max_length=16, choices=Engine.choices)
    environment = models.CharField(
        "环境", max_length=16, choices=Environment.choices, default=Environment.PROD
    )
    description = models.TextField("备注", blank=True, default="")

    class Meta:
        db_table = "cluster"
        verbose_name = "集群"
        verbose_name_plural = "集群"
        ordering = ["id"]
        constraints = [
            # 同一部门下集群名不重复；不同部门可以有同名集群（如都叫 order-db）
            models.UniqueConstraint(
                fields=["department", "name"], name="uniq_cluster_name_per_department"
            )
        ]
        indexes = [models.Index(fields=["department", "environment"])]

    def __str__(self) -> str:
        return f"{self.name}[{self.environment}]"


class DBInstance(TimeStampedModel):
    """数据库实例：一个可 TCP 访问的 host:port。"""

    class Role(models.TextChoices):
        PRIMARY = "primary", "主库"
        REPLICA = "replica", "从库"
        STANDALONE = "standalone", "单机"

    class Status(models.TextChoices):
        ONLINE = "online", "在线"
        OFFLINE = "offline", "离线"
        UNKNOWN = "unknown", "未知"

    cluster = models.ForeignKey(
        Cluster, verbose_name="所属集群", on_delete=models.CASCADE, related_name="instances"
    )
    name = models.CharField("实例名称", max_length=64)
    host = models.CharField("主机地址", max_length=255, help_text="IP 或域名")
    port = models.PositiveIntegerField(
        "端口", validators=[MinValueValidator(1), MaxValueValidator(65535)]
    )
    role = models.CharField(
        "角色", max_length=16, choices=Role.choices, default=Role.STANDALONE
    )
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.UNKNOWN, db_index=True
    )
    version = models.CharField("版本号", max_length=32, blank=True, default="")

    # 最近一次探活结果，便于列表页直接展示而不必触发探测
    last_probe_at = models.DateTimeField("最近探测时间", null=True, blank=True)
    last_probe_ok = models.BooleanField("最近探测是否可达", null=True, blank=True)
    last_probe_latency_ms = models.FloatField("最近探测耗时(ms)", null=True, blank=True)
    last_probe_error = models.CharField("最近探测错误", max_length=255, blank=True, default="")

    class Meta:
        db_table = "db_instance"
        verbose_name = "数据库实例"
        verbose_name_plural = "数据库实例"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["cluster", "name"], name="uniq_instance_name_in_cluster"),
            models.CheckConstraint(
                condition=models.Q(port__gte=1) & models.Q(port__lte=65535),
                name="ck_instance_port_range",
            ),
            # 全库范围内 host:port 唯一。同一对地址出现在两个集群里，
            # 本质上就是同一台实例，属于登记错误，从根上拦掉。
            models.UniqueConstraint(fields=["host", "port"], name="uniq_instance_endpoint"),
        ]
        indexes = [
            models.Index(fields=["cluster", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.name}@{self.host}:{self.port}"

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def department_id(self) -> int:
        return self.cluster.department_id


class InstanceCredential(TimeStampedModel):
    """实例账号凭据（只存密文，按版本留痕）。

    版本语义：同一 (instance, username) 下 version 单调递增，
    有且仅有一条 is_current=True 的记录代表「当前有效密码」。
    """

    instance = models.ForeignKey(
        DBInstance, verbose_name="实例", on_delete=models.CASCADE, related_name="credentials"
    )
    username = models.CharField("账号", max_length=64, default="root")
    password_cipher = models.TextField("密码密文", help_text="Fernet 密文，禁止存明文")
    version = models.PositiveIntegerField("版本号", default=1)
    is_current = models.BooleanField("是否当前版本", default=True, db_index=True)
    rotated_at = models.DateTimeField("最近轮换时间", default=timezone.now, db_index=True)

    # 轮换失败留痕：密码没换成功时，旧凭据仍是 current，但要知道上次尝试失败过
    last_attempt_at = models.DateTimeField("最近轮换尝试时间", null=True, blank=True)
    last_error = models.CharField("最近一次轮换错误", max_length=500, blank=True, default="")

    class Meta:
        db_table = "instance_credential"
        verbose_name = "实例凭据"
        verbose_name_plural = "实例凭据"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["instance", "username", "version"], name="uniq_credential_version"
            ),
            # 部分唯一索引：保证每个账号只有一条 current。
            # MySQL 不支持带条件的唯一索引，Django 会跳过创建，
            # 此时由服务层的行锁 + 事务保证，见 services/rotation.py。
            models.UniqueConstraint(
                fields=["instance", "username"],
                condition=models.Q(is_current=True),
                name="uniq_current_credential_per_account",
            ),
        ]
        indexes = [
            models.Index(fields=["is_current", "rotated_at"], name="idx_cred_rotation_due"),
        ]

    def __str__(self) -> str:
        return f"{self.instance_id}:{self.username} v{self.version}"

    # -- 加解密 -------------------------------------------------------------
    def set_password(self, raw_password: str) -> None:
        """只写内存字段，调用方负责 save()，避免半截写入。"""
        self.password_cipher = get_cipher().encrypt(raw_password)

    def reveal_password(self) -> str:
        """解密出明文。调用点必须受权限保护并写审计日志。"""
        return get_cipher().decrypt(self.password_cipher)

    @property
    def masked_password(self) -> str:
        return "********"


class CredentialAccessLog(models.Model):
    """凭据读取审计：谁、在什么时候、从哪个 IP 看了哪个账号的明文密码。

    安全要求里「能看」和「看过要留痕」必须成对出现，否则无法追责。
    """

    class Action(models.TextChoices):
        REVEAL = "reveal", "查看明文"
        ROTATE = "rotate", "轮换"
        CREATE = "create", "创建"

    credential = models.ForeignKey(
        InstanceCredential, on_delete=models.CASCADE, related_name="access_logs"
    )
    actor = models.CharField("操作人", max_length=64)
    action = models.CharField("动作", max_length=16, choices=Action.choices)
    ip = models.GenericIPAddressField("来源 IP", null=True, blank=True)
    user_agent = models.CharField("UA", max_length=255, blank=True, default="")
    created_at = models.DateTimeField("发生时间", auto_now_add=True, db_index=True)

    class Meta:
        db_table = "credential_access_log"
        verbose_name = "凭据访问日志"
        verbose_name_plural = "凭据访问日志"
        ordering = ["-id"]


class InstanceStatDaily(models.Model):
    """按「日期 × 部门 × 集群」聚合的实例数量快照。

    独立成表而不是实时算，是为了：
      * 报表查询不扫实例大表
      * 保留历史时点数据（实例被删后历史仍可追溯）
    """

    stat_date = models.DateField("统计日期", db_index=True)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="daily_stats"
    )
    cluster = models.ForeignKey(Cluster, on_delete=models.PROTECT, related_name="daily_stats")

    # 维表名称快照：部门/集群改名后，历史报表仍显示当时的名字
    department_name = models.CharField("部门名称快照", max_length=64)
    cluster_name = models.CharField("集群名称快照", max_length=64)

    total_count = models.PositiveIntegerField("实例总数", default=0)
    online_count = models.PositiveIntegerField("在线数", default=0)
    offline_count = models.PositiveIntegerField("离线数", default=0)
    unknown_count = models.PositiveIntegerField("未知数", default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "instance_stat_daily"
        verbose_name = "实例数量日报"
        verbose_name_plural = "实例数量日报"
        ordering = ["-stat_date", "department_id", "cluster_id"]
        constraints = [
            # 幂等基石：同一天同一集群只有一行，任务重跑走更新而不是追加
            models.UniqueConstraint(
                fields=["stat_date", "cluster"], name="uniq_daily_stat_per_cluster"
            )
        ]

    def __str__(self) -> str:
        return f"{self.stat_date} {self.department_name}/{self.cluster_name}={self.total_count}"
