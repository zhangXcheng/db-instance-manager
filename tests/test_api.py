"""CRUD 与权限测试。"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from inventory.models import (
    CredentialAccessLog,
    DBInstance,
    Department,
    InstanceCredential,
)
from tests.factories import make_cluster, make_credential, make_department, make_instance


class BaseApiTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user("tester", password="pass12345")
        self.client.force_authenticate(self.user)


class AuthTests(TestCase):
    def test_login_returns_token(self):
        User.objects.create_user("alice", password="Passw0rd#1")
        client = APIClient()
        response = client.post(
            "/api/auth/login/",
            {"username": "alice", "password": "Passw0rd#1"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.data)

    def test_login_rejects_bad_password(self):
        User.objects.create_user("bob", password="Passw0rd#1")
        client = APIClient()
        response = client.post(
            "/api/auth/login/", {"username": "bob", "password": "wrong"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_anonymous_is_rejected(self):
        client = APIClient()
        response = client.get("/api/departments/")
        self.assertIn(response.status_code, (401, 403))


class DepartmentApiTests(BaseApiTestCase):
    def test_crud(self):
        response = self.client.post(
            reverse("department-list"), {"name": "市场部", "code": "MKT"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        pk = response.data["id"]

        response = self.client.get(reverse("department-detail", args=[pk]))
        self.assertEqual(response.data["name"], "市场部")

        response = self.client.patch(
            reverse("department-detail", args=[pk]), {"owner": "张三"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["owner"], "张三")

        response = self.client.delete(reverse("department-detail", args=[pk]))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Department.objects.filter(pk=pk).exists())

    def test_parent_cannot_be_self(self):
        department = make_department()
        response = self.client.patch(
            reverse("department-detail", args=[department.pk]),
            {"parent": department.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_duplicate_code_rejected(self):
        make_department("技术部", "TECH")
        response = self.client.post(
            reverse("department-list"), {"name": "技术二部", "code": "TECH"}, format="json"
        )
        self.assertEqual(response.status_code, 400)


class ClusterApiTests(BaseApiTestCase):
    def setUp(self):
        super().setUp()
        self.department = make_department()

    def test_crud(self):
        payload = {
            "name": "order-db",
            "code": "ORDER",
            "department": self.department.pk,
            "engine": "mysql",
            "environment": "prod",
        }
        response = self.client.post(reverse("cluster-list"), payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["department_name"], self.department.name)

        pk = response.data["id"]
        response = self.client.get(reverse("cluster-detail", args=[pk]))
        self.assertEqual(response.data["instance_count"], 0)

    def test_filter_by_department(self):
        other = make_department("财务部", "FIN")
        make_cluster(self.department, "order-db", "ORDER")
        make_cluster(other, "finance-db", "FIND")

        response = self.client.get(
            reverse("cluster-list"), {"department": self.department.pk}
        )
        self.assertEqual(response.data["count"], 1)


class InstanceApiTests(BaseApiTestCase):
    def setUp(self):
        super().setUp()
        self.department = make_department()
        self.cluster = make_cluster(self.department)

    def test_crud_and_default_status(self):
        payload = {
            "name": "order-db-01",
            "cluster": self.cluster.pk,
            "host": "10.0.0.11",
            "port": 3306,
            "role": "primary",
        }
        response = self.client.post(reverse("instance-list"), payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], DBInstance.Status.UNKNOWN)
        self.assertEqual(response.data["endpoint"], "10.0.0.11:3306")
        self.assertEqual(response.data["department_name"], self.department.name)

    def test_invalid_port_rejected(self):
        payload = {
            "name": "bad",
            "cluster": self.cluster.pk,
            "host": "10.0.0.12",
            "port": 70000,
        }
        response = self.client.post(reverse("instance-list"), payload, format="json")
        self.assertEqual(response.status_code, 400)

    def test_host_with_scheme_rejected(self):
        payload = {
            "name": "bad",
            "cluster": self.cluster.pk,
            "host": "http://10.0.0.12",
            "port": 3306,
        }
        response = self.client.post(reverse("instance-list"), payload, format="json")
        self.assertEqual(response.status_code, 400)

    def test_duplicate_endpoint_rejected(self):
        make_instance(self.cluster, "i1", "10.0.0.13", 3306)
        response = self.client.post(
            reverse("instance-list"),
            {"name": "i2", "cluster": self.cluster.pk, "host": "10.0.0.13", "port": 3306},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_filter_by_status_and_search(self):
        make_instance(
            self.cluster, "order-01", "10.0.0.14", 3306, status=DBInstance.Status.ONLINE
        )
        make_instance(
            self.cluster, "user-01", "10.0.0.15", 3306, status=DBInstance.Status.OFFLINE
        )

        response = self.client.get(reverse("instance-list"), {"status": "online"})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["name"], "order-01")

        response = self.client.get(reverse("instance-list"), {"search": "user"})
        self.assertEqual(response.data["count"], 1)


class CredentialApiTests(BaseApiTestCase):
    def setUp(self):
        super().setUp()
        self.cluster = make_cluster(make_department())
        self.instance = make_instance(self.cluster)
        self.credential = make_credential(self.instance, password="Plain#Text9")

    def test_list_never_exposes_password(self):
        response = self.client.get(reverse("credential-list"))
        self.assertEqual(response.status_code, 200)

        item = response.data["results"][0]
        self.assertEqual(item["masked_password"], "********")
        self.assertNotIn("password_cipher", item)
        self.assertNotIn("password", item)
        # 连响应体的原始 JSON 都不该出现明文
        self.assertNotIn("Plain#Text9", response.content.decode())
        self.assertNotIn("gAAAA", response.content.decode())  # Fernet 密文前缀

    def test_reveal_requires_staff(self):
        response = self.client.post(reverse("credential-reveal", args=[self.credential.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CredentialAccessLog.objects.exists())

    def test_reveal_by_staff_is_audited(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])

        response = self.client.post(reverse("credential-reveal", args=[self.credential.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["password"], "Plain#Text9")

        log = CredentialAccessLog.objects.get()
        self.assertEqual(log.actor, "tester")
        self.assertEqual(log.action, CredentialAccessLog.Action.REVEAL)

    def test_credential_is_read_only(self):
        response = self.client.post(
            reverse("credential-list"),
            {"instance": self.instance.pk, "username": "hacker"},
            format="json",
        )
        self.assertEqual(response.status_code, 405)
        self.assertEqual(InstanceCredential.objects.count(), 1)

    def test_access_logs_endpoint(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.client.post(reverse("credential-reveal", args=[self.credential.pk]))

        response = self.client.get(
            reverse("credential-access-logs", args=[self.credential.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)


class RotateApiTests(BaseApiTestCase):
    def setUp(self):
        super().setUp()
        from unittest.mock import patch

        self.cluster = make_cluster(make_department())
        self.instance = make_instance(self.cluster)
        make_credential(self.instance)

        patcher = patch("inventory.services.rotation.get_driver")
        self.driver = patcher.start()
        self.addCleanup(patcher.stop)

    def test_rotate_requires_staff(self):
        response = self.client.post(
            reverse("instance-rotate-password", args=[self.instance.pk])
        )
        self.assertEqual(response.status_code, 403)

    def test_rotate_as_staff(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])

        response = self.client.post(
            reverse("instance-rotate-password", args=[self.instance.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["status"], "rotated")
        self.assertEqual(
            self.instance.credentials.filter(is_current=True).get().version, 2
        )


class DemoPageTests(TestCase):
    def test_demo_page_renders(self):
        """演示页由 Django 模板渲染，不能因为模板语法错误 500。"""
        response = self.client.get("/demo/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "数据库实例管理系统")
        self.assertContains(response, "/api/instances/")


class ApiDocsTests(TestCase):
    def test_openapi_schema_available(self):
        """schema 必须能生成，且覆盖到探测、轮换等自定义 action。"""
        response = self.client.get("/api/schema/")
        self.assertEqual(response.status_code, 200)

        body = response.content.decode("utf-8")
        self.assertIn("/api/instances/", body)
        self.assertIn("probe", body)

    def test_swagger_ui_renders(self):
        response = self.client.get("/api/docs/")
        self.assertEqual(response.status_code, 200)
