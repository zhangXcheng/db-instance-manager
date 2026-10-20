"""路由。

/api/departments/            部门 CRUD
/api/clusters/               集群 CRUD
/api/instances/              实例 CRUD
/api/instances/{id}/probe/           POST 探测单实例 TCP 可达性
/api/instances/probe-batch/          POST 批量探测
/api/instances/{id}/rotate-password/ POST 手动轮换密码（管理员）
/api/credentials/            凭据列表（只读、不含密码）
/api/credentials/{id}/reveal/ POST 查看明文（管理员 + 审计）
/api/stats/daily/            每日统计查询
/api/stats/daily/by-department/ 按部门汇总
/api/stats/daily/rebuild/    POST 补跑统计（管理员）
/api/auth/login/             换取 Token
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from inventory.views import (
    ClusterViewSet,
    DailyStatViewSet,
    DBInstanceViewSet,
    DepartmentViewSet,
    InstanceCredentialViewSet,
    LoginView,
)

router = DefaultRouter()
router.register("departments", DepartmentViewSet, basename="department")
router.register("clusters", ClusterViewSet, basename="cluster")
router.register("instances", DBInstanceViewSet, basename="instance")
router.register("credentials", InstanceCredentialViewSet, basename="credential")
router.register("stats/daily", DailyStatViewSet, basename="daily-stat")

urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("", include(router.urls)),
]
