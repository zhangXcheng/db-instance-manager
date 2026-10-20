"""定时任务与调度配置测试。

重点是锁住「调度必须用 crontab」这条约束：
写成 24 * 60 * 60 秒也能跑，但 beat 进程重启后执行时间会整体漂移，
是个不报错、却会让「每天 00:00 出报表」静默失效的坑。
"""
from datetime import date
from unittest.mock import patch

from celery.schedules import crontab
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from inventory.models import InstanceStatDaily
from inventory.tasks import aggregate_daily_instance_stats, rotate_all_passwords
from tests.factories import make_cluster, make_credential, make_department, make_instance


class BeatScheduleTests(TestCase):
    def test_schedule_uses_crontab(self):
        schedule = settings.CELERY_BEAT_SCHEDULE

        rotate = schedule["rotate-instance-passwords"]["schedule"]
        stats = schedule["aggregate-daily-instance-stats"]["schedule"]

        self.assertIsInstance(rotate, crontab)
        self.assertIsInstance(stats, crontab)

        # 00:00 与 12:00
        self.assertEqual(sorted(rotate.hour), [0, 12])
        self.assertEqual(rotate.minute, {0})

        # 每天 00:00
        self.assertEqual(stats.hour, {0})
        self.assertEqual(stats.minute, {0})


class RotateTaskTests(TestCase):
    def test_task_returns_summary(self):
        instance = make_instance(make_cluster(make_department()))
        make_credential(instance)

        with patch("inventory.services.rotation.get_driver"):
            result = rotate_all_passwords(force=True)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["summary"], {"rotated": 1})
        self.assertEqual(result["failures"], [])

    def test_task_reports_failures(self):
        instance = make_instance(make_cluster(make_department()))
        make_credential(instance)

        with patch("inventory.services.rotation.get_driver") as driver:
            driver.return_value.alter_password.side_effect = RuntimeError("boom")
            result = rotate_all_passwords(force=True)

        self.assertEqual(result["summary"], {"failed": 1})
        self.assertEqual(len(result["failures"]), 1)
        self.assertIn("boom", result["failures"][0]["error"])


class AggregateTaskTests(TestCase):
    def test_task_writes_stats(self):
        cluster = make_cluster(make_department())
        make_instance(cluster)

        result = aggregate_daily_instance_stats("2026-10-19")

        self.assertEqual(result["stat_date"], "2026-10-19")
        self.assertEqual(result["cluster_rows"], 1)
        self.assertTrue(
            InstanceStatDaily.objects.filter(stat_date=date(2026, 10, 19)).exists()
        )


class BatchProbePayloadTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user("tester", password="pass12345")
        self.client.force_authenticate(self.user)

        cluster = make_cluster(make_department())
        make_instance(cluster, "i1", "10.0.0.1", 3306)

    def test_empty_ids_means_nothing(self):
        response = self.client.post(
            reverse("instance-probe-batch"), {"ids": []}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    @patch("inventory.services.tcp_probe.socket.create_connection")
    def test_missing_ids_means_all(self, mock_conn):
        mock_conn.side_effect = ConnectionRefusedError()

        response = self.client.post(reverse("instance-probe-batch"), {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
