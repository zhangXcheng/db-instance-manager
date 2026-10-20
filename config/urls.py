"""URL 路由总入口。"""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from django.views.generic import TemplateView


def healthz(_request):
    """健康检查：不经过认证、不写日志，供 LB / K8s 探针使用。"""
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="healthz"),
    # 单页演示控制台：由 Django 直接渲染，避免额外引入前端构建链
    path("demo/", TemplateView.as_view(template_name="demo.html"), name="demo"),
    path("api/", include("inventory.urls")),
]
