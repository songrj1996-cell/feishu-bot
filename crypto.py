import base64
import hashlib

from Crypto.Cipher import AES


class AESCipher:
    def __init__(self, key: str) -> None:
        self.key = hashlib.sha256(key.encode("utf-8")).digest()

    def decrypt(self, encrypted: str) -> str:
        raw = base64.b64decode(encrypted)
        iv, body = raw[:16], raw[16:]
        decrypted = AES.new(self.key, AES.MODE_CBC, iv).decrypt(body)
        pad = decrypted[-1]
        return decrypted[:-pad].decode("utf-8")
