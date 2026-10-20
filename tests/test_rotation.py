"""密码轮换测试。

覆盖点：版本递进、旧版失效、驱动失败不污染本地、dry-run、
补偿回滚、审计留痕、幂等跳过。
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from inventory.models import CredentialAccessLog, InstanceCredential
from inventory.services.rotation import (
    DRY_RUN,
    FAILED,
    ROTATED,
    SKIPPED,
    generate_password,
    rotate_due_credentials,
    rotate_instance,
)
from tests.factories import make_cluster, make_credential, make_department, make_instance

INITIAL_PASSWORD = "Init#Passw0rd"


class GeneratePasswordTests(TestCase):
    def test_length_and_char_classes(self):
        for _ in range(30):
            password = generate_password(16)
            self.assertEqual(len(password), 16)
            self.assertTrue(any(c.islower() for c in password))
            self.assertTrue(any(c.isupper() for c in password))
            self.assertTrue(any(c.isdigit() for c in password))
            self.assertTrue(any(not c.isalnum() for c in password))

    def test_no_collision(self):
        passwords = {generate_password(24) for _ in range(100)}
        self.assertEqual(len(passwords), 100)


class RotationTests(TestCase):
    def setUp(self):
        self.department = make_department()
        self.cluster = make_cluster(self.department)
        self.instance = make_instance(self.cluster)
        self.credential = make_credential(self.instance, password=INITIAL_PASSWORD)

        self.driver = MagicMock()
        patcher = patch("inventory.services.rotation.get_driver", return_value=self.driver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rotate_creates_new_version(self):
        outcomes = rotate_due_credentials(force=True)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, ROTATED)
        self.assertEqual(outcomes[0].version, 2)

        self.credential.refresh_from_db()
        self.assertFalse(self.credential.is_current)

        new_credential = InstanceCredential.objects.get(
            instance=self.instance, is_current=True
        )
        self.assertEqual(new_credential.version, 2)
        self.assertEqual(new_credential.last_error, "")

        # 驱动拿到的新密码必须与落库密文一致，否则远端和本地就脱节了
        called = self.driver.alter_password.call_args.kwargs
        self.assertEqual(called["current_password"], INITIAL_PASSWORD)
        self.assertEqual(called["new_password"], new_credential.reveal_password())
        self.assertEqual(len(called["new_password"]), settings.CREDENTIAL_PASSWORD_LENGTH)

        # 审计留痕
        self.assertTrue(
            CredentialAccessLog.objects.filter(
                credential=new_credential, action=CredentialAccessLog.Action.ROTATE
            ).exists()
        )

    def test_not_due_is_not_even_selected(self):
        """未到期的凭据在 SQL 阶段就被过滤掉，不会进入轮换流程，节省遍历开销。"""
        outcomes = rotate_due_credentials(force=False)
        self.assertEqual(outcomes, [])
        self.assertEqual(self.instance.credentials.count(), 1)
        self.driver.alter_password.assert_not_called()

    def test_skipped_when_taken_by_concurrent_worker(self):
        """模拟并发：拿到锁时发现已被别的 worker 轮换过，应跳过而不是再改一次密码。"""
        stale = InstanceCredential.objects.get(pk=self.credential.pk)
        stale.is_current = False  # 内存里模拟「已被他人轮换」

        with patch(
            "inventory.services.rotation._lock_credential", return_value=stale
        ):
            outcomes = rotate_due_credentials(force=True)

        self.assertEqual(outcomes[0].status, SKIPPED)
        self.driver.alter_password.assert_not_called()

    def test_due_after_interval_is_rotated(self):
        InstanceCredential.objects.filter(pk=self.credential.pk).update(
            rotated_at=timezone.now()
            - timedelta(hours=settings.CREDENTIAL_ROTATION_INTERVAL_HOURS + 1)
        )
        outcomes = rotate_due_credentials()
        self.assertEqual(outcomes[0].status, ROTATED)

    def test_driver_failure_keeps_local_intact(self):
        self.driver.alter_password.side_effect = RuntimeError("connection refused")

        outcomes = rotate_due_credentials(force=True)

        self.assertEqual(outcomes[0].status, FAILED)
        self.assertIn("connection refused", outcomes[0].error)

        self.credential.refresh_from_db()
        self.assertTrue(self.credential.is_current)  # 旧密码仍然有效
        self.assertIn("connection refused", self.credential.last_error)
        self.assertEqual(self.instance.credentials.count(), 1)

    def test_dry_run_does_not_apply(self):
        outcomes = rotate_due_credentials(force=True, dry_run=True)

        self.assertEqual(outcomes[0].status, DRY_RUN)
        self.assertEqual(self.instance.credentials.count(), 1)
        self.driver.alter_password.assert_not_called()

    def test_compensation_restores_remote_password(self):
        """远端改成功但本地落库失败 → 应把远端密码补偿回旧值。"""
        with patch(
            "inventory.services.rotation._promote_new_version",
            side_effect=RuntimeError("db unavailable"),
        ):
            outcomes = rotate_due_credentials(force=True)

        self.assertEqual(outcomes[0].status, FAILED)
        self.assertEqual(self.driver.alter_password.call_count, 2)

        compensation = self.driver.alter_password.call_args_list[1].kwargs
        self.assertEqual(compensation["new_password"], INITIAL_PASSWORD)

        self.credential.refresh_from_db()
        self.assertTrue(self.credential.is_current)

    def test_manual_rotate_ignores_interval(self):
        outcomes = rotate_instance(self.instance, force=True)
        self.assertEqual(outcomes[0].status, ROTATED)

    def test_multiple_accounts_rotate_independently(self):
        make_credential(self.instance, username="readonly", password="Read#Only1")

        outcomes = rotate_due_credentials(force=True)

        self.assertEqual(len(outcomes), 2)
        self.assertEqual({o.username for o in outcomes}, {"root", "readonly"})
        self.assertEqual(
            InstanceCredential.objects.filter(instance=self.instance, is_current=True).count(), 2
        )
