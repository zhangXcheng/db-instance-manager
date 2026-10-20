"""加密与模型层测试。"""
from django.test import TestCase

from core.crypto import SecretCipher, get_cipher
from inventory.models import InstanceCredential
from tests.factories import make_cluster, make_credential, make_department, make_instance


class SecretCipherTests(TestCase):
    def test_encrypt_decrypt_roundtrip(self):
        cipher = SecretCipher(["unit-test-secret"])
        token = cipher.encrypt("P@ssw0rd-中文")
        self.assertNotIn("P@ssw0rd", token)  # 密文里不能出现明文片段
        self.assertEqual(cipher.decrypt(token), "P@ssw0rd-中文")

    def test_same_plaintext_produces_different_cipher(self):
        """Fernet 带随机 IV，相同明文每次密文不同，避免密文可被比对。"""
        cipher = SecretCipher(["unit-test-secret"])
        self.assertNotEqual(cipher.encrypt("abc"), cipher.encrypt("abc"))

    def test_multi_key_supports_rotation(self):
        """旧密钥加密的数据，换成新密钥列表后仍能解开（密钥轮换不断服）。"""
        old_cipher = SecretCipher(["old-secret"])
        token = old_cipher.encrypt("legacy")

        rotated = SecretCipher(["new-secret", "old-secret"])
        self.assertEqual(rotated.decrypt(token), "legacy")

    def test_try_decrypt_returns_none_on_garbage(self):
        cipher = SecretCipher(["unit-test-secret"])
        self.assertIsNone(cipher.try_decrypt("not-a-valid-token"))


class CredentialModelTests(TestCase):
    def test_password_not_stored_in_plaintext(self):
        department = make_department()
        cluster = make_cluster(department)
        instance = make_instance(cluster)
        credential = make_credential(instance, password="PlainText#123")

        stored = InstanceCredential.objects.get(pk=credential.pk)
        self.assertNotIn("PlainText#123", stored.password_cipher)
        self.assertEqual(stored.reveal_password(), "PlainText#123")
        self.assertEqual(stored.masked_password, "********")

    def test_cipher_is_usable_from_settings(self):
        # 确保 get_cipher() 能正确读取 settings.FERNET_SECRETS
        token = get_cipher().encrypt("x")
        self.assertEqual(get_cipher().decrypt(token), "x")
