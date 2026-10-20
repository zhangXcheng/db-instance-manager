"""URL 路由总入口。"""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from django.views.generic import TemplateView
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView


def healthz(_request):
    """健康检查：不经过认证、不写日志，供 LB / K8s 探针使用。"""
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="healthz"),
    # 单页演示控制台：由 Django 直接渲染，避免额外引入前端构建链
    path("demo/", TemplateView.as_view(template_name="demo.html"), name="demo"),
    # OpenAPI 文档。注意必须放在 "api/" include 之前，否则会被 Router 吃掉
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    path("api/", include("inventory.urls")),
]
