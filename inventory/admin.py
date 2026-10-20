"""Admin 后台。

密码密文不放进表单，避免运维在后台误填明文导致数据污染。
"""
from django.contrib import admin

from inventory.models import (
    Cluster,
    CredentialAccessLog,
    DBInstance,
    Department,
    InstanceCredential,
    InstanceStatDaily,
)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "code", "parent", "owner", "created_at")
    search_fields = ("name", "code", "owner")
    list_filter = ("parent",)


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "code", "department", "engine", "environment")
    list_filter = ("engine", "environment", "department")
    search_fields = ("name", "code")


@admin.register(DBInstance)
class DBInstanceAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "host", "port", "cluster", "role", "status", "last_probe_at")
    list_filter = ("status", "role", "cluster__environment", "cluster__engine")
    search_fields = ("name", "host")
    readonly_fields = ("last_probe_at", "last_probe_ok", "last_probe_latency_ms", "last_probe_error")


@admin.register(InstanceCredential)
class InstanceCredentialAdmin(admin.ModelAdmin):
    list_display = ("id", "instance", "username", "version", "is_current", "rotated_at")
    list_filter = ("is_current",)
    search_fields = ("instance__name", "username")
    exclude = ("password_cipher",)  # 密文/明文都不在后台展示
    readonly_fields = ("rotated_at", "last_attempt_at", "last_error")


@admin.register(CredentialAccessLog)
class CredentialAccessLogAdmin(admin.ModelAdmin):
    list_display = ("id", "credential", "actor", "action", "ip", "created_at")
    list_filter = ("action",)
    readonly_fields = [f.name for f in CredentialAccessLog._meta.fields]


@admin.register(InstanceStatDaily)
class InstanceStatDailyAdmin(admin.ModelAdmin):
    list_display = (
        "stat_date",
        "department_name",
        "cluster_name",
        "total_count",
        "online_count",
        "offline_count",
        "unknown_count",
    )
    list_filter = ("stat_date", "department")
    search_fields = ("department_name", "cluster_name")
