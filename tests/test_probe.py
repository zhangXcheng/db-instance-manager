"""TCP 探测测试（全部 mock socket，不依赖外网）。"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from inventory.models import DBInstance
from inventory.services.tcp_probe import probe_tcp
from tests.factories import make_cluster, make_department, make_instance


class ProbeServiceTests(TestCase):
    @patch("inventory.services.tcp_probe.socket.create_connection")
    def test_reachable(self, mock_conn):
        mock_conn.return_value.__enter__.return_value = MagicMock()
        result = probe_tcp("10.0.0.1", 3306)
        self.assertTrue(result.reachable)
        self.assertIsNone(result.error)
        self.assertIsNotNone(result.latency_ms)

    @patch("inventory.services.tcp_probe.socket.create_connection")
    def test_timeout(self, mock_conn):
        mock_conn.side_effect = TimeoutError()
        result = probe_tcp("10.0.0.1", 3306)
        self.assertFalse(result.reachable)
        self.assertIn("超时", result.error)

    @patch("inventory.services.tcp_probe.socket.create_connection")
    def test_connection_refused(self, mock_conn):
        mock_conn.side_effect = ConnectionRefusedError()
        result = probe_tcp("10.0.0.1", 3306)
        self.assertFalse(result.reachable)
        self.assertIn("拒绝", result.error)


class ProbeApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        from django.contrib.auth.models import User

        self.user = User.objects.create_user("tester", password="pass12345")
        self.client.force_authenticate(self.user)

        department = make_department()
        cluster = make_cluster(department)
        self.instance = make_instance(cluster)

    @patch("inventory.services.tcp_probe.socket.create_connection")
    def test_probe_api_updates_status(self, mock_conn):
        mock_conn.return_value.__enter__.return_value = MagicMock()

        response = self.client.post(reverse("instance-probe", args=[self.instance.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["reachable"])

        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, DBInstance.Status.ONLINE)
        self.assertTrue(self.instance.last_probe_ok)
        self.assertIsNotNone(self.instance.last_probe_at)

    @patch("inventory.services.tcp_probe.socket.create_connection")
    def test_probe_failure_marks_offline(self, mock_conn):
        mock_conn.side_effect = ConnectionRefusedError()

        response = self.client.post(reverse("instance-probe", args=[self.instance.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["reachable"])

        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, DBInstance.Status.OFFLINE)

    @patch("inventory.services.tcp_probe.socket.create_connection")
    def test_batch_probe(self, mock_conn):
        mock_conn.return_value.__enter__.return_value = MagicMock()

        response = self.client.post(
            reverse("instance-probe-batch"),
            {"ids": [self.instance.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertTrue(response.data["results"][0]["reachable"])

    def test_anonymous_cannot_probe(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(reverse("instance-probe", args=[self.instance.pk]))
        self.assertIn(response.status_code, (401, 403))
