"""WeCom callback protocol: SHA1 signature + AES-256-CBC encrypt/decrypt."""
import base64
import hashlib
import time
import random
import string
import struct
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def verify_signature(token, timestamp, nonce, msg, signature):
    parts = sorted([token, timestamp, nonce, msg])
    expected = hashlib.sha1("".join(parts).encode()).hexdigest()
    return expected == signature


def _create_aes_cipher(aes_key):
    iv = aes_key[:16]
    return Cipher(algorithms.AES(aes_key), modes.CBC(iv))


def decrypt_message(encrypted_b64, aes_key):
    raw = base64.b64decode(encrypted_b64)
    cipher = _create_aes_cipher(aes_key)
    decryptor = cipher.decryptor()
    padded = decryptor.update(raw) + decryptor.finalize()
    pad_len = padded[-1]
    return padded[:-pad_len]


def encrypt_message(plaintext, aes_key):
    # WeCom uses non-standard PKCS7 with block size 32 (not 16)
    block_size = 32
    pad_len = block_size - (len(plaintext) % block_size)
    if pad_len == 0:
        pad_len = block_size
    padded = plaintext + bytes([pad_len] * pad_len)
    cipher = _create_aes_cipher(aes_key)
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_callback(encrypted_b64, msg_signature, timestamp, nonce, token, aes_key):
    if not verify_signature(token, timestamp, nonce, encrypted_b64, msg_signature):
        raise ValueError("Signature verification failed")
    raw = base64.b64decode(encrypted_b64)
    cipher = _create_aes_cipher(aes_key)
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(raw) + decryptor.finalize()
    # WeCom format: 16-byte random + 4-byte network byte order length + plaintext + corpid + padding
    msg_len = int.from_bytes(decrypted[16:20], "big")
    return decrypted[20:20 + msg_len].decode("utf-8")


def encrypt_callback(plaintext, token, aes_key, corpid="corpid"):
    random_bytes = "".join(random.choices(string.ascii_letters, k=16)).encode()
    msg_len = struct.pack(">I", len(plaintext.encode()))
    content = random_bytes + msg_len + plaintext.encode() + corpid.encode()
    encrypted = encrypt_message(content, aes_key)
    encrypted_b64 = base64.b64encode(encrypted).decode()
    timestamp = str(int(time.time()))
    nonce = "".join(random.choices(string.digits, k=8))
    parts = sorted([token, timestamp, nonce, encrypted_b64])
    signature = hashlib.sha1("".join(parts).encode()).hexdigest()
    return {
        "encrypted": encrypted_b64,
        "signature": signature,
        "timestamp": timestamp,
        "nonce": nonce,
    }