"""
tests.test_security
~~~~~~~~~~~~~~~~~~~
Unit tests for the security module.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import security as sec


class TestSessionHmac:
    def test_sign_verify_roundtrip(self, monkeypatch):
        monkeypatch.setattr(sec, "SESSION_HMAC_SECRET", "super-secret")
        raw = "user-session-12345"
        signed = sec.sign_session_id(raw)
        assert "." in signed
        verified = sec.verify_session_id(signed)
        assert verified == raw

    def test_tampered_signature_rejected(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setattr(sec, "SESSION_HMAC_SECRET", "super-secret")
        signed = sec.sign_session_id("user-xyz")
        tampered = signed[:-3] + "AAA"
        with pytest.raises(HTTPException) as exc:
            sec.verify_session_id(tampered)
        assert exc.value.status_code == 400

    def test_no_secret_passthrough(self, monkeypatch):
        monkeypatch.setattr(sec, "SESSION_HMAC_SECRET", None)
        raw = "plain-session"
        assert sec.sign_session_id(raw) == raw
        assert sec.verify_session_id(raw) == raw

    def test_missing_dot_rejected(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setattr(sec, "SESSION_HMAC_SECRET", "secret")
        with pytest.raises(HTTPException):
            sec.verify_session_id("no-dot-here")


class TestTokenBucket:
    def test_allows_within_capacity(self):
        bucket = sec._TokenBucket(capacity=10, refill_window=60)
        for _ in range(10):
            assert bucket.consume("ip-1") is True

    def test_blocks_over_capacity(self):
        bucket = sec._TokenBucket(capacity=3, refill_window=60)
        for _ in range(3):
            bucket.consume("ip-1")
        assert bucket.consume("ip-1") is False

    def test_different_keys_independent(self):
        bucket = sec._TokenBucket(capacity=1, refill_window=60)
        bucket.consume("ip-A")
        assert bucket.consume("ip-A") is False
        assert bucket.consume("ip-B") is True   # separate bucket
