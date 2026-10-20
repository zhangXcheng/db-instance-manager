"""管理命令测试。

`seed_demo` 不只是造数据——它还要负责把演示账号建好，
否则面试官 clone 下来是空库，没有账号就进不了控制台。
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from inventory.models import Cluster, DBInstance, Department


class SeedDemoCommandTests(TestCase):
    def _run(self) -> str:
        output = StringIO()
        call_command("seed_demo", stdout=output)
        return output.getvalue()

    def test_creates_demo_user_and_data(self):
        output = self._run()

        user = get_user_model().objects.get(username="demo")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password("Demo#2026"))

        self.assertEqual(Department.objects.count(), 2)
        self.assertEqual(Cluster.objects.count(), 5)
        self.assertEqual(DBInstance.objects.count(), 6)
        self.assertTrue(DBInstance.objects.filter(name="demo-self").exists())
        self.assertIn("demo", output)

    def test_idempotent(self):
        self._run()
        self._run()

        self.assertEqual(Department.objects.count(), 2)
        self.assertEqual(DBInstance.objects.count(), 6)
        self.assertEqual(get_user_model().objects.filter(username="demo").count(), 1)

    def test_does_not_reset_existing_password(self):
        """重复执行不能把用户改过的密码冲掉。"""
        user = get_user_model().objects.create_user("demo", password="MyOwnPass#1")
        self._run()
        user.refresh_from_db()
        self.assertTrue(user.check_password("MyOwnPass#1"))

    def test_promotes_existing_user_to_admin(self):
        get_user_model().objects.create_user("demo", password="x")
        self._run()

        user = get_user_model().objects.get(username="demo")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_custom_demo_port(self):
        """runserver 换了端口时，demo-self 也要跟着指过去。"""
        output = StringIO()
        call_command("seed_demo", stdout=output, port=8123)

        instance = DBInstance.objects.get(name="demo-self")
        self.assertEqual(instance.port, 8123)
        self.assertEqual(instance.host, "127.0.0.1")
