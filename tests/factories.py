"""测试数据构造工具。"""
from __future__ import annotations

from django.utils import timezone

from inventory.models import Cluster, DBInstance, Department, InstanceCredential


def make_department(name: str = "技术部", code: str = "TECH", **kwargs) -> Department:
    return Department.objects.create(name=name, code=code, **kwargs)


def make_cluster(department: Department, name: str = "order-db", code: str = "ORDER", **kwargs) -> Cluster:
    return Cluster.objects.create(
        name=name,
        code=code,
        department=department,
        engine=kwargs.pop("engine", Cluster.Engine.MYSQL),
        environment=kwargs.pop("environment", Cluster.Environment.PROD),
        **kwargs,
    )


def make_instance(cluster: Cluster, name: str = "order-db-01", host: str = "10.0.0.1", port: int = 3306, **kwargs) -> DBInstance:
    return DBInstance.objects.create(
        cluster=cluster, name=name, host=host, port=port, **kwargs
    )


def make_credential(
    instance: DBInstance, username: str = "root", password: str = "Init#Passw0rd", version: int = 1
) -> InstanceCredential:
    credential = InstanceCredential(
        instance=instance,
        username=username,
        version=version,
        is_current=True,
        rotated_at=timezone.now(),
    )
    credential.set_password(password)
    credential.save()
    return credential
