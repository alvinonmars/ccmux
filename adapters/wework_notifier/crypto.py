"""AES decryption and signature verification for WeChat Work callbacks.

WeChat Work uses AES-256-CBC with a custom padding/framing scheme:
  plaintext = 16 random bytes + 4-byte msg length (big-endian) + message + receiveid
"""
from __future__ import annotations

import base64
import hashlib
import struct

from Crypto.Cipher import AES


def verify_signature(
    token: str, timestamp: str, nonce: str, encrypt_msg: str, msg_signature: str
) -> bool:
    """Verify SHA1 signature from WeChat Work callback.

    Signature = SHA1(sort([token, timestamp, nonce, encrypt_msg]))
    """
    items = sorted([token, timestamp, nonce, encrypt_msg])
    expected = hashlib.sha1("".join(items).encode()).hexdigest()
    return expected == msg_signature


def decrypt_message(aes_key_b64: str, ciphertext: str, corp_id: str) -> str:
    """Decrypt AES-256-CBC encrypted message from WeChat Work.

    Args:
        aes_key_b64: Base64-encoded AES key (43 chars from EncodingAESKey).
        ciphertext: Base64-encoded ciphertext from <Encrypt> element.
        corp_id: Expected CorpID for verification.

    Returns:
        Decrypted message content (XML string for messages, plain text for echostr).

    Raises:
        ValueError: If CorpID in decrypted payload doesn't match.
    """
    aes_key = base64.b64decode(aes_key_b64 + "=")
    cipher = AES.new(aes_key, AES.MODE_CBC, iv=aes_key[:16])
    encrypted = base64.b64decode(ciphertext)
    plaintext = cipher.decrypt(encrypted)

    # Remove PKCS#7 padding
    pad_len = plaintext[-1]
    plaintext = plaintext[:-pad_len]

    # Skip 16 random bytes, read 4-byte message length, extract message
    msg_len = struct.unpack(">I", plaintext[16:20])[0]
    msg = plaintext[20 : 20 + msg_len].decode("utf-8")
    recv_id = plaintext[20 + msg_len :].decode("utf-8")

    if recv_id != corp_id:
        raise ValueError(f"CorpID mismatch: expected {corp_id}, got {recv_id}")

    return msg
