"""密码轮换服务。

这是整套系统里最容易写出「看起来对、实际会炸」的一段，核心难点有三个：

1）**一致性**：改密码是跨系统的两步操作（目标库 + 自己的库）。
   只成功一半，业务就连不上库了。所以：
     - 顺序必须是「先改远端，成功后再落本地」
     - 本地落库失败时，尝试把远端改回旧密码（补偿事务 Saga）
     - 补偿也失败则打 CRITICAL 日志，交人工核对，绝不假装成功

2）**幂等**：Celery 任务可能重复投递/重试，beat 也可能抖动。
   所以进入临界区后必须二次校验「是否真的到期」，而不是信任任务参数。

3）**并发**：多 worker 同时轮换同一个实例，会把远端密码改成两个不同的值。
   用数据库行锁（select_for_update）串行化。SQLite 不支持行锁，
   自动降级为无锁（仅限本地单进程演示），生产必须用 MySQL/PG。

关于长事务：网络调用被包在事务里会持有行锁若干秒。
量级上可接受（改一条密码），但生产更推荐 Redis 分布式锁 + 短事务，
或者两阶段（先写 pending 记录 → 改远端 → 确认），可参考 README。
"""
from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from inventory.models import DBInstance, InstanceCredential
from inventory.services.drivers import get_driver

logger = logging.getLogger("inventory.rotation")

LOWERCASE = string.ascii_lowercase
UPPERCASE = string.ascii_uppercase
DIGITS = string.digits
SPECIALS = "!@#$%^&*()-_=+[]{}"
ALPHABET = LOWERCASE + UPPERCASE + DIGITS + SPECIALS

# 状态常量
ROTATED = "rotated"
SKIPPED = "skipped"
FAILED = "failed"
DRY_RUN = "dry_run"


def generate_password(length: int | None = None) -> str:
    """生成随机强密码。

    用「先填四类字符、再补齐、最后打乱」的构造式生成，
    而不是 while 随机重试——后者在 length 较小时可能长时间撞不出结果。
    """
    length = max(length or settings.CREDENTIAL_PASSWORD_LENGTH, 8)
    chars = [
        secrets.choice(LOWERCASE),
        secrets.choice(UPPERCASE),
        secrets.choice(DIGITS),
        secrets.choice(SPECIALS),
    ]
    chars.extend(secrets.choice(ALPHABET) for _ in range(length - len(chars)))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


@dataclass
class RotationOutcome:
    instance_id: int
    instance_name: str
    username: str
    status: str
    version: int | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "username": self.username,
            "status": self.status,
            "version": self.version,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def rotate_due_credentials(
    *,
    force: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[RotationOutcome]:
    """扫描到期的凭据并逐一轮换。返回逐条结果，便于任务侧汇总与告警。"""
    now = now or timezone.now()
    cutoff = now - timedelta(hours=settings.CREDENTIAL_ROTATION_INTERVAL_HOURS)

    queryset = (
        InstanceCredential.objects.filter(is_current=True)
        .select_related("instance")
        .order_by("rotated_at", "id")
    )
    if not force:
        queryset = queryset.filter(rotated_at__lte=cutoff)
    if limit:
        queryset = queryset[:limit]

    outcomes: list[RotationOutcome] = []
    for candidate in list(queryset):
        outcomes.append(_rotate_one(candidate, force=force, dry_run=dry_run, now=now))
    return outcomes


def rotate_instance(
    instance: DBInstance, *, force: bool = True, dry_run: bool = False
) -> list[RotationOutcome]:
    """手动轮换指定实例的全部当前账号（供 API 调用）。"""
    now = timezone.now()
    return [
        _rotate_one(cred, force=force, dry_run=dry_run, now=now)
        for cred in instance.credentials.filter(is_current=True).select_related("instance")
    ]


# ---------------------------------------------------------------------------
# 单条轮换
# ---------------------------------------------------------------------------
def _rotate_one(
    candidate: InstanceCredential, *, force: bool, dry_run: bool, now: datetime
) -> RotationOutcome:
    instance = candidate.instance
    old_password: str | None = None
    new_password: str | None = None
    remote_changed = False
    username = candidate.username

    try:
        with transaction.atomic():
            # 1) 进临界区：加行锁并重新读取，后续所有判断都基于最新数据
            fresh = _lock_credential(candidate.pk)
            if fresh is None:
                return _outcome(instance, username, SKIPPED, error="记录不存在或正被其他任务处理")

            instance = fresh.instance
            username = fresh.username

            # 2) 幂等校验：不信任调用方，按最新落库时间再判断一次
            if not fresh.is_current:
                return _outcome(instance, username, SKIPPED, error="已被其他任务轮换")
            cutoff = now - timedelta(hours=settings.CREDENTIAL_ROTATION_INTERVAL_HOURS)
            if not force and fresh.rotated_at > cutoff:
                return _outcome(
                    instance, username, SKIPPED, error="未到轮换周期", version=fresh.version
                )

            old_password = fresh.reveal_password()
            new_password = generate_password()

            if dry_run:
                logger.info("dry-run 轮换 instance=%s user=%s", instance.pk, username)
                return _outcome(instance, username, DRY_RUN, version=fresh.version)

            # 3) 先改远端。失败会直接抛出，本地数据保持不动，二者仍一致
            get_driver().alter_password(
                host=instance.host,
                port=instance.port,
                username=username,
                current_password=old_password,
                new_password=new_password,
            )
            remote_changed = True

            # 4) 远端成功后，在同一个事务里完成「旧版作废 + 新版生效」
            new_version = _promote_new_version(fresh, new_password, now)

            logger.info(
                "密码轮换成功 instance=%s endpoint=%s user=%s v%s->v%s",
                instance.pk,
                instance.endpoint,
                username,
                fresh.version,
                new_version,
            )
            return _outcome(instance, username, ROTATED, version=new_version)

    except Exception as exc:  # noqa: BLE001 - 需要兜住所有异常并保证状态可观测
        # 远端已改但本地未落库（例如提交阶段失败）→ 尝试改回旧密码，避免业务失联
        if remote_changed and old_password and new_password:
            _compensate(instance, username, new_password, old_password)

        message = f"{type(exc).__name__}: {exc}"
        _record_failure(candidate.pk, message, now)
        logger.error(
            "密码轮换失败 instance=%s user=%s err=%s", instance.pk, username, message
        )
        return _outcome(instance, username, FAILED, error=message[:500])


def _lock_credential(pk: int) -> InstanceCredential | None:
    """按数据库能力选择加锁方式。

    skip_locked=True：已被其他 worker 锁住的行直接跳过，不排队等，
    避免大批量轮换时任务互相堵死。
    """
    queryset = InstanceCredential.objects.select_related("instance")
    if connection.features.has_select_for_update:
        queryset = queryset.select_for_update(
            skip_locked=connection.features.has_select_for_update_skip_locked
        )
    try:
        return queryset.get(pk=pk)
    except InstanceCredential.DoesNotExist:
        return None


def _promote_new_version(
    credential: InstanceCredential, new_password: str, now: datetime
) -> int:
    """旧版本置为非当前 → 写入新版本 → 返回新版本号。调用方保证在事务内。"""
    max_version = (
        InstanceCredential.objects.filter(
            instance_id=credential.instance_id, username=credential.username
        ).aggregate(value=Max("version"))["value"]
        or 0
    )
    next_version = max_version + 1

    # 用 update() 而不是 save()：避免把内存里 is_current=True 的旧对象写回去
    InstanceCredential.objects.filter(pk=credential.pk).update(
        is_current=False, last_attempt_at=now, last_error=""
    )

    new_credential = InstanceCredential(
        instance_id=credential.instance_id,
        username=credential.username,
        version=next_version,
        is_current=True,
        rotated_at=now,
        last_attempt_at=now,
    )
    new_credential.set_password(new_password)
    new_credential.save(force_insert=True)

    _write_audit_log(new_credential, credential.instance)
    return next_version


def _write_audit_log(credential: InstanceCredential, instance: DBInstance) -> None:
    """轮换属于敏感操作，系统自动执行也要留痕。"""
    from inventory.models import CredentialAccessLog

    CredentialAccessLog.objects.create(
        credential=credential,
        actor="system:celery",
        action=CredentialAccessLog.Action.ROTATE,
        user_agent=f"rotation-task instance={instance.pk}",
    )


def _record_failure(credential_pk: int, message: str, now: datetime) -> None:
    """失败只更新「尝试时间/错误」，不动 rotated_at，让下个周期能继续重试。"""
    InstanceCredential.objects.filter(pk=credential_pk).update(
        last_attempt_at=now, last_error=message[:500]
    )


def _compensate(
    instance: DBInstance, username: str, new_password: str, old_password: str
) -> None:
    """补偿：把远端密码改回旧值。"""
    try:
        get_driver().alter_password(
            host=instance.host,
            port=instance.port,
            username=username,
            current_password=new_password,
            new_password=old_password,
        )
        logger.warning("已补偿回滚 instance=%s user=%s 的密码", instance.pk, username)
    except Exception:  # noqa: BLE001
        logger.critical(
            "补偿失败！instance=%s endpoint=%s 远端密码可能已漂移，需人工核对",
            instance.pk,
            instance.endpoint,
            exc_info=True,
        )


def _outcome(
    instance: DBInstance,
    username: str,
    status: str,
    version: int | None = None,
    error: str | None = None,
) -> RotationOutcome:
    return RotationOutcome(
        instance_id=instance.pk,
        instance_name=instance.name,
        username=username,
        status=status,
        version=version,
        error=error,
    )
