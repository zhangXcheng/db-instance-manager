"""改密驱动。

轮换密码有一个绕不开的现实：**必须真的把新密码改到目标数据库上**。
只生成随机串存进自己的库，是自嗨——目标库上还是旧密码，业务会直接连不上。

所以抽象出 Driver 接口：
  * SimulatedDriver：本地演示用，不碰真实数据库（默认）
  * MySQLDriver：真实执行 ALTER USER，需要 pymysql

换别的数据库只需实现 alter_password 并通过配置切换，不动业务代码。
"""
from __future__ import annotations

import logging
from typing import Protocol

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger("inventory.driver")


class PasswordDriver(Protocol):
    def alter_password(
        self,
        *,
        host: str,
        port: int,
        username: str,
        current_password: str,
        new_password: str,
        timeout: float = 5.0,
    ) -> None: ...


class SimulatedDriver:
    """演示驱动：只做校验与日志，不产生真实副作用。

    故意不打印密码本身，只在日志里留长度和指纹，防止明文进入日志系统。
    """

    def alter_password(
        self,
        *,
        host: str,
        port: int,
        username: str,
        current_password: str,
        new_password: str,
        timeout: float = 5.0,
    ) -> None:
        if not new_password:
            raise ValueError("新密码不能为空")
        logger.info(
            "simulated alter_password host=%s port=%s user=%s len=%s",
            host,
            port,
            username,
            len(new_password),
        )


class MySQLDriver:
    """真实改密实现（示例）。

    凭据流转：用「当前密码」登录 → ALTER USER 改成「新密码」。
    new_password 走参数绑定，username 做标识符转义，避免 SQL 注入。
    """

    def alter_password(
        self,
        *,
        host: str,
        port: int,
        username: str,
        current_password: str,
        new_password: str,
        timeout: float = 5.0,
    ) -> None:
        try:
            import pymysql  # 延迟导入，未装 pymysql 时不影响其他功能
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("使用 MySQLDriver 需要先 pip install pymysql") from exc

        safe_user = username.replace("'", "''").replace("\\", "\\\\")
        conn = pymysql.connect(
            host=host,
            port=int(port),
            user=username,
            password=current_password,
            connect_timeout=timeout,
            read_timeout=timeout,
            write_timeout=timeout,
            charset="utf8mb4",
        )
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"ALTER USER '{safe_user}'@'%' IDENTIFIED BY %s", (new_password,)
                )
            conn.commit()
        finally:
            conn.close()


def get_driver() -> PasswordDriver:
    """按配置实例化驱动；配置写错时尽早抛错，而不是静默用错实现。"""
    driver_path = settings.CREDENTIAL_DRIVER
    driver_cls = import_string(driver_path)
    return driver_cls()
