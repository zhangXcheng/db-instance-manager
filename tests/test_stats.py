"""每日统计测试。"""
from datetime import date

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from inventory.models import DBInstance, InstanceStatDaily
from inventory.services.stats import build_daily_stats
from tests.factories import make_cluster, make_department, make_instance

STAT_DATE = date(2026, 10, 20)


class DailyStatsTests(TestCase):
    def setUp(self):
        self.tech = make_department("技术部", "TECH")
        self.finance = make_department("财务部", "FIN")

        self.order_cluster = make_cluster(self.tech, "order-db", "ORDER")
        self.user_cluster = make_cluster(self.tech, "user-db", "USER")
        self.finance_cluster = make_cluster(self.finance, "finance-db", "FIND")

        make_instance(self.order_cluster, "i1", "10.0.0.1", 3306, status=DBInstance.Status.ONLINE)
        make_instance(self.order_cluster, "i2", "10.0.0.2", 3306, status=DBInstance.Status.ONLINE)
        make_instance(self.user_cluster, "i3", "10.0.0.3", 3306, status=DBInstance.Status.OFFLINE)
        make_instance(self.user_cluster, "i4", "10.0.0.4", 3306, status=DBInstance.Status.UNKNOWN)
        # finance_cluster 故意不放实例，验证 0 值行

    def test_aggregate_by_department_and_cluster(self):
        summary = build_daily_stats(STAT_DATE)
        self.assertEqual(summary["cluster_rows"], 3)
        self.assertEqual(summary["total_instances"], 4)

        rows = {
            row.cluster.code: row
            for row in InstanceStatDaily.objects.filter(stat_date=STAT_DATE)
        }
        self.assertEqual(rows["ORDER"].total_count, 2)
        self.assertEqual(rows["ORDER"].online_count, 2)
        self.assertEqual(rows["ORDER"].department_name, "技术部")

        self.assertEqual(rows["USER"].total_count, 2)
        self.assertEqual(rows["USER"].offline_count, 1)
        self.assertEqual(rows["USER"].unknown_count, 1)

        # 没有实例的集群也要有一行 0 值，否则报表断层
        self.assertEqual(rows["FIND"].total_count, 0)

    def test_idempotent_when_rerun(self):
        build_daily_stats(STAT_DATE)
        build_daily_stats(STAT_DATE)
        self.assertEqual(
            InstanceStatDaily.objects.filter(stat_date=STAT_DATE).count(), 3
        )

    def test_rerun_updates_existing_rows(self):
        build_daily_stats(STAT_DATE)

        # i3 由 offline 变为 online → 该集群 online=1, offline=0, unknown=1
        DBInstance.objects.filter(name="i3").update(status=DBInstance.Status.ONLINE)
        build_daily_stats(STAT_DATE)

        row = InstanceStatDaily.objects.get(stat_date=STAT_DATE, cluster=self.user_cluster)
        self.assertEqual(row.online_count, 1)
        self.assertEqual(row.offline_count, 0)
        self.assertEqual(row.unknown_count, 1)
        self.assertEqual(InstanceStatDaily.objects.filter(stat_date=STAT_DATE).count(), 3)

    def test_snapshot_survives_rename(self):
        build_daily_stats(STAT_DATE)
        self.tech.name = "技术中心"
        self.tech.save(update_fields=["name"])

        row = InstanceStatDaily.objects.get(stat_date=STAT_DATE, cluster=self.order_cluster)
        self.assertEqual(row.department_name, "技术部")  # 历史报表保留当时的名字


class DailyStatsApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        from django.contrib.auth.models import User

        self.user = User.objects.create_user("tester", password="pass12345")
        self.client.force_authenticate(self.user)

        department = make_department()
        cluster = make_cluster(department)
        make_instance(cluster, status=DBInstance.Status.ONLINE)
        build_daily_stats(STAT_DATE)

    def test_list_stats(self):
        response = self.client.get(reverse("daily-stat-list"), {"stat_date": STAT_DATE.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["total_count"], 1)

    def test_by_department(self):
        response = self.client.get(
            reverse("daily-stat-by-department"), {"stat_date": STAT_DATE.isoformat()}
        )
        self.assertEqual(response.status_code, 200)
        row = response.data["results"][0]
        self.assertEqual(row["total_count"], 1)
        self.assertEqual(row["cluster_count"], 1)

    def test_rebuild_requires_staff(self):
        response = self.client.post(reverse("daily-stat-rebuild"), {}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_rebuild_by_staff(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        response = self.client.post(
            reverse("daily-stat-rebuild"),
            {"stat_date": STAT_DATE.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["cluster_rows"], 1)
