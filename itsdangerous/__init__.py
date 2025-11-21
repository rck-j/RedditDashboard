"""Minimal subset of the ``itsdangerous`` API used by Starlette sessions.

This lightweight implementation avoids pulling the full dependency during
testing while remaining compatible with ``SessionMiddleware``'s expectations.
It is **not** intended for production-grade cryptographic signing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from .exc import BadSignature


def want_bytes(value, encoding: str = "utf-8", errors: str = "strict") -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode(encoding, errors)


class Signer:
    def __init__(self, secret_key: str | bytes, salt: str | bytes | None = None, sep: str = ".") -> None:
        self.secret_key = want_bytes(secret_key)
        self.sep = want_bytes(sep)
        self.salt = want_bytes(salt or "itsdangerous.Signer")

    def derive_key(self) -> bytes:
        return hmac.new(self.secret_key, self.salt, hashlib.sha1).digest()

    def get_signature(self, value: bytes) -> bytes:
        sig = hmac.new(self.derive_key(), value, hashlib.sha1).digest()
        return base64.urlsafe_b64encode(sig).rstrip(b"=")

    def sign(self, value: str | bytes) -> bytes:
        value_bytes = want_bytes(value)
        return value_bytes + self.sep + self.get_signature(value_bytes)

    def unsign(self, signed_value: str | bytes, max_age: int | None = None) -> bytes:  # noqa: ARG002
        signed_bytes = want_bytes(signed_value)
        value, sep, sig = signed_bytes.rpartition(self.sep)
        if not sep:
            raise BadSignature("No signature found")
        expected = self.get_signature(value)
        if not hmac.compare_digest(sig, expected):
            raise BadSignature("Signature mismatch")
        return value


class TimestampSigner(Signer):
    def sign(self, value: str | bytes) -> bytes:
        value_bytes = want_bytes(value)
        timestamp = want_bytes(int(time.time()))
        value_with_timestamp = value_bytes + self.sep + timestamp
        return value_with_timestamp + self.sep + self.get_signature(
            value_with_timestamp
        )

    def unsign(self, signed_value: str | bytes, max_age: int | None = None) -> bytes:
        signed_bytes = want_bytes(signed_value)
        value_and_timestamp, sep, sig = signed_bytes.rpartition(self.sep)
        if not sep:
            raise BadSignature("No signature found")
        value, timestamp_sep, timestamp = value_and_timestamp.rpartition(self.sep)
        if not timestamp_sep:
            raise BadSignature("Timestamp missing")
        expected = self.get_signature(value_and_timestamp)
        if not hmac.compare_digest(sig, expected):
            raise BadSignature("Signature mismatch")
        return value


__all__ = ["BadSignature", "Signer", "TimestampSigner", "want_bytes"]
