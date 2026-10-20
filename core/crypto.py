"""字段级加密。

为什么用 Fernet 而不是自己拼 AES：
  * Fernet = AES-128-CBC + HMAC-SHA256，天然带完整性校验（防篡改、防密文伪造）
  * 自带时间戳，可用于审计
  * 使用 MultiFernet 支持「多密钥共存」，实现密钥轮换而不用停机重刷全表

生产建议：密钥由 KMS / Vault 下发，进程内存持有，绝不落库、不进代码仓库。
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings


class SecretCipher:
    """对称加密门面，屏蔽密钥派生与多密钥细节。"""

    def __init__(self, secrets: list[str]):
        if not secrets:
            raise ValueError("至少需要一个加密密钥")
        self._fernet = MultiFernet([Fernet(self._derive_key(s)) for s in secrets])

    @staticmethod
    def _derive_key(secret: str) -> bytes:
        """把任意长度的口令派生成 Fernet 需要的 32 字节 urlsafe-base64 密钥。

        如果传入的本身已经是合法 Fernet key（44 字符 base64，解出 32 字节），
        直接使用，方便运维直接下发标准密钥。
        """
        raw = secret.encode()
        try:
            if len(raw) == 44 and len(base64.urlsafe_b64decode(raw)) == 32:
                return raw
        except Exception:  # noqa: BLE001 - 非 base64 就走派生逻辑
            pass
        return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            raise ValueError("明文不能为 None")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")

    def try_decrypt(self, token: str) -> str | None:
        """解密失败返回 None，用于兼容历史脏数据而不让整个接口 500。"""
        try:
            return self.decrypt(token)
        except (InvalidToken, ValueError):
            return None


@lru_cache(maxsize=1)
def get_cipher() -> SecretCipher:
    """懒加载单例。

    不在 module 级实例化，避免 Django 尚未完成 settings 初始化时导入报错。
    """
    return SecretCipher(settings.FERNET_SECRETS)
