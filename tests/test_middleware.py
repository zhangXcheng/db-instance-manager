"""请求耗时中间件测试。"""
from django.test import TestCase


class RequestTimingMiddlewareTests(TestCase):
    def test_duration_header_present(self):
        response = self.client.get("/api/departments/")
        # 未登录会是 401，但中间件仍应记录耗时
        self.assertIn("X-Request-Duration-ms", response)
        self.assertGreaterEqual(float(response["X-Request-Duration-ms"]), 0.0)

    def test_healthz_is_exempt(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("X-Request-Duration-ms", response)

    def test_duration_recorded_on_exception(self):
        """视图抛异常时也要留痕，且异常必须继续向上抛（不能被中间件吞掉）。"""
        from unittest.mock import patch

        from django.contrib.auth.models import User

        # 必须先登录，否则 401 在 DRF 的 initial() 阶段就返回了，根本进不到视图
        user = User.objects.create_user("timing", password="pass12345")
        self.client.force_login(user)

        with patch(
            "inventory.views.DepartmentViewSet.list",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs("core.timing", level="WARNING") as captured:
                with self.assertRaises(RuntimeError):
                    self.client.get("/api/departments/")

        self.assertTrue(any("request failed" in line for line in captured.output))
