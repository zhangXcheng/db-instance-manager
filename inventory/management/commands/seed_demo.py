"""生成演示数据与演示账号：python manage.py seed_demo

幂等，可重复执行。

为什么连账号一起建：`db.sqlite3` 不入库（里面是本地数据），
面试官 clone 下来是空库。若还要他自己 `createsuperuser` 设密码，
`/demo/` 和 `/api/docs/` 的调试链路就断了。演示数据本就应该包含「能登录的账号」。

密码可用环境变量 `DEMO_PASSWORD` 覆盖；生产环境不要执行本命令。
"""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from inventory.models import Cluster, DBInstance, Department, InstanceCredential
from inventory.services.rotation import generate_password
from inventory.services.stats import build_daily_stats

DEMO_USERNAME = "demo"
DEFAULT_DEMO_PASSWORD = "Demo#2026"


class Command(BaseCommand):
    help = "生成部门 / 集群 / 实例 / 凭据的演示数据，并创建演示账号"

    @transaction.atomic
    def handle(self, *args, **options):
        departments = [
            ("技术部", "TECH", "张伟"),
            ("财务部", "FIN", "李娜"),
        ]
        department_map = {}
        for name, code, owner in departments:
            department, _ = Department.objects.get_or_create(
                code=code, defaults={"name": name, "owner": owner}
            )
            department_map[code] = department

        clusters = [
            ("订单主库", "ORDER-MYSQL", "TECH", Cluster.Engine.MYSQL, Cluster.Environment.PROD),
            ("用户中心", "USER-MYSQL", "TECH", Cluster.Engine.MYSQL, Cluster.Environment.PROD),
            ("缓存集群", "CACHE-REDIS", "TECH", Cluster.Engine.REDIS, Cluster.Environment.STAGING),
            ("财务库", "FIN-MYSQL", "FIN", Cluster.Engine.MYSQL, Cluster.Environment.PROD),
            # 专门放一个「本机可探测」的实例，否则演示数据全是内网 IP，
            # 点探测清一色超时，看不出成功路径长什么样
            ("演示环境", "DEMO", "TECH", Cluster.Engine.MYSQL, Cluster.Environment.DEV),
        ]
        cluster_map = {}
        for name, code, department_code, engine, environment in clusters:
            cluster, _ = Cluster.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "department": department_map[department_code],
                    "engine": engine,
                    "environment": environment,
                },
            )
            cluster_map[code] = cluster

        instances = [
            ("order-db-01", "ORDER-MYSQL", "10.0.1.11", 3306, DBInstance.Role.PRIMARY, DBInstance.Status.ONLINE),
            ("order-db-02", "ORDER-MYSQL", "10.0.1.12", 3306, DBInstance.Role.REPLICA, DBInstance.Status.ONLINE),
            ("user-db-01", "USER-MYSQL", "10.0.2.11", 3306, DBInstance.Role.PRIMARY, DBInstance.Status.OFFLINE),
            ("cache-01", "CACHE-REDIS", "10.0.3.11", 6379, DBInstance.Role.STANDALONE, DBInstance.Status.ONLINE),
            ("finance-db-01", "FIN-MYSQL", "10.0.4.11", 3306, DBInstance.Role.PRIMARY, DBInstance.Status.UNKNOWN),
            # 指向本机 Django 服务，runserver 起来后探测这个实例就是可达的
            ("demo-self", "DEMO", "127.0.0.1", 8000, DBInstance.Role.STANDALONE, DBInstance.Status.UNKNOWN),
        ]
        created_credentials = 0
        for name, cluster_code, host, port, role, status in instances:
            instance, created = DBInstance.objects.get_or_create(
                cluster=cluster_map[cluster_code],
                name=name,
                defaults={"host": host, "port": port, "role": role, "status": status},
            )
            if created or not instance.credentials.exists():
                credential = InstanceCredential(
                    instance=instance,
                    username="root",
                    version=1,
                    is_current=True,
                    rotated_at=timezone.now(),
                )
                credential.set_password(generate_password())
                credential.save()
                created_credentials += 1

        summary = build_daily_stats()
        password, user_created = self._ensure_demo_user()

        self.stdout.write(
            self.style.SUCCESS(
                f"演示数据就绪：部门 {Department.objects.count()} 个，"
                f"集群 {Cluster.objects.count()} 个，实例 {DBInstance.objects.count()} 个，"
                f"新建凭据 {created_credentials} 条"
            )
        )
        self.stdout.write(f"今日统计：{summary}")
        self.stdout.write("")

        line = "=" * 60
        self.stdout.write(self.style.SUCCESS(line))
        if user_created:
            self.stdout.write(
                self.style.SUCCESS(f"  演示账号  {DEMO_USERNAME} / {password}   （管理员）")
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"  演示账号  {DEMO_USERNAME} 已存在，密码未改动"
                    f"（忘记可执行 manage.py changepassword {DEMO_USERNAME}）"
                )
            )
        self.stdout.write("  控制台    http://127.0.0.1:8000/demo/")
        self.stdout.write("  接口文档  http://127.0.0.1:8000/api/docs/")
        self.stdout.write("  后台      http://127.0.0.1:8000/admin/")
        self.stdout.write(self.style.SUCCESS(line))
        self.stdout.write(
            "提示：runserver 起来后，对 demo-self（127.0.0.1:8000）点探测即可看到「可达」"
        )
        self.stdout.write("下一步：python manage.py runserver")

    def _ensure_demo_user(self) -> tuple[str, bool]:
        """创建（或补权）演示账号。已存在时不动密码，避免覆盖用户自己改过的。"""
        User = get_user_model()
        password = os.getenv("DEMO_PASSWORD", DEFAULT_DEMO_PASSWORD)

        user, created = User.objects.get_or_create(
            username=DEMO_USERNAME,
            defaults={
                "email": "demo@example.com",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if created:
            user.set_password(password)
            user.save(update_fields=["password"])
            return password, True

        # 老账号可能权限不足（比如被手动改过），补齐但不改密码
        changed = []
        if not user.is_staff:
            user.is_staff = True
            changed.append("is_staff")
        if not user.is_superuser:
            user.is_superuser = True
            changed.append("is_superuser")
        if changed:
            user.save(update_fields=changed)
        return password, False
