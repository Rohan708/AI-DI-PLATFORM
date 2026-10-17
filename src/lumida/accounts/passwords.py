"""Password hashing with scrypt from Python's standard library (no extra dependency).

Stored form: ``scrypt$<n>$<r>$<p>$<salt>$<hash>`` (base64), so the cost can be raised
later and old hashes still verify.
"""

import base64
import hashlib
import hmac
import secrets
from functools import cache

# Cost: about 32 MB of memory and ~0.1 s per check; slow enough to make guessing expensive.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024


class WeakPasswordError(ValueError):
    pass


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"a password needs at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError(f"a password can have at most {MAX_PASSWORD_LENGTH} characters")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * n * r, dklen=KEY_BYTES
    )


def hash_password(password: str) -> str:
    check_password_rules(password)
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    b64 = base64.b64encode
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt).decode()}${b64(key).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        if scheme != "scrypt" or len(password) > MAX_PASSWORD_LENGTH:
            return False
        derived = _derive(password, base64.b64decode(salt), int(n), int(r), int(p))
    except ValueError:
        return False
    return hmac.compare_digest(derived, base64.b64decode(key))


# Checked against when the email is unknown, so a wrong email takes as long as a wrong
# password (no way to find out which emails have accounts).
@cache
def dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))
