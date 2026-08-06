"""Tests for WeCom callback crypto."""
import os
import sys
import base64
import hashlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wecom_server.crypto import (
    verify_signature,
    decrypt_message,
    encrypt_message,
    decrypt_callback,
    encrypt_callback,
)

TOKEN = "test_token"
TIMESTAMP = "1234567890"
NONCE = "test_nonce"


def test_verify_signature():
    msg = "hello"
    parts = sorted([TOKEN, TIMESTAMP, NONCE, msg])
    expected = hashlib.sha1("".join(parts).encode()).hexdigest()
    assert verify_signature(TOKEN, TIMESTAMP, NONCE, msg, expected)


def test_verify_signature_wrong():
    assert not verify_signature(TOKEN, TIMESTAMP, NONCE, "hello", "wrongsig")


def test_encrypt_decrypt_roundtrip():
    key = b"\x00" * 32
    plain = b"hello wecom"
    encrypted = encrypt_message(plain, key)
    # decrypt_message expects base64-encoded input
    decrypted = decrypt_message(base64.b64encode(encrypted).decode(), key)
    assert decrypted == plain


def test_decrypt_callback_fails_on_bad_signature():
    key = b"\x00" * 32
    import json
    try:
        decrypt_callback("fake_encrypted", "bad_sig", TIMESTAMP, NONCE, TOKEN, key)
        assert False, "Should have raised"
    except ValueError:
        pass


def test_encrypt_callback_roundtrip():
    key = b"\x00" * 32
    plaintext = "测试消息"
    result = encrypt_callback(plaintext, TOKEN, key)
    assert "encrypted" in result
    assert "signature" in result
    assert "timestamp" in result
    assert "nonce" in result
    # Verify signature
    parts = sorted([TOKEN, result["timestamp"], result["nonce"], result["encrypted"]])
    expected_sig = hashlib.sha1("".join(parts).encode()).hexdigest()
    assert result["signature"] == expected_sig