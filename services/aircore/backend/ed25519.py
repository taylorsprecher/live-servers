"""
Ed25519 signatures — pure-Python implementation of RFC 8032.

Increment 2 replaces Increment 1's HMAC-SHA256 stand-in with the real
algorithm the AirShield spec mandates (policy.yaml: signature.algorithm =
ed25519). No third-party crypto library is available in this environment
(no PyNaCl / cryptography, and pip is externally managed), so this module
implements Ed25519 directly from RFC 8032 using only the standard library
(hashlib for SHA-512).

Properties / honesty notes:
  * Algorithmically genuine Ed25519: key derivation, signing and
    verification follow RFC 8032 exactly and are checked against the
    RFC 8032 section 7.1 test vectors in test_increment2.py.
  * Pure Python is SLOW (tens of ms per operation) and NOT constant-time.
    It is suitable for this local demo platform only; a production build
    should swap in a vetted library (PyNaCl / cryptography) behind this
    same small API: keypair_from_seed, publickey, sign, verify.
  * Keys are 32-byte seeds; public keys and signatures are handled as
    bytes internally and hex strings at API boundaries.
"""

from __future__ import annotations

import hashlib

# --- Field / curve constants (RFC 8032 section 5.1) -------------------------
_Q = 2 ** 255 - 19                                    # field prime
_L = 2 ** 252 + 27742317777372353535851937790883648493  # group order
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q         # Edwards curve d
_I = pow(2, (_Q - 1) // 4, _Q)                        # sqrt(-1) mod q


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


# Base point B.
_BY = (4 * pow(5, _Q - 2, _Q)) % _Q
_BX = _xrecover(_BY)
_B = (_BX % _Q, _BY % _Q, 1, (_BX * _BY) % _Q)  # extended coordinates (X,Y,Z,T)

_IDENTITY = (0, 1, 1, 0)


# --- Point arithmetic in extended twisted-Edwards coordinates ---------------
# Formulas from RFC 8032's reference implementation (Hisil et al.).

def _point_add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _Q
    Bv = (P[1] + P[0]) * (Q[1] + Q[0]) % _Q
    C = 2 * P[3] * Q[3] * _D % _Q
    Dv = 2 * P[2] * Q[2] % _Q
    E = (Bv - A) % _Q
    F = (Dv - C) % _Q
    G = (Dv + C) % _Q
    H = (Bv + A) % _Q
    return (E * F % _Q, G * H % _Q, F * G % _Q, E * H % _Q)


def _point_double(P):
    A = P[0] * P[0] % _Q
    Bv = P[1] * P[1] % _Q
    C = 2 * P[2] * P[2] % _Q
    Dv = (-A) % _Q
    E = ((P[0] + P[1]) * (P[0] + P[1]) - A - Bv) % _Q
    G = (Dv + Bv) % _Q
    F = (G - C) % _Q
    H = (Dv - Bv) % _Q
    return (E * F % _Q, G * H % _Q, F * G % _Q, E * H % _Q)


def _scalarmult(P, e: int):
    Q = _IDENTITY
    while e > 0:
        if e & 1:
            Q = _point_add(Q, P)
        P = _point_double(P)
        e >>= 1
    return Q


def _point_equal(P, Q) -> bool:
    return ((P[0] * Q[2] - Q[0] * P[2]) % _Q == 0
            and (P[1] * Q[2] - Q[1] * P[2]) % _Q == 0)


def _encode_point(P) -> bytes:
    x, y, z = P[0], P[1], P[2]
    zinv = pow(z, _Q - 2, _Q)
    xa, ya = x * zinv % _Q, y * zinv % _Q
    bits = [(ya >> i) & 1 for i in range(255)] + [xa & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(32))


def _decode_point(data: bytes):
    if len(data) != 32:
        raise ValueError("encoded point must be 32 bytes")
    y = sum(2 ** i * ((data[i // 8] >> (i % 8)) & 1) for i in range(255))
    sign = (data[31] >> 7) & 1
    x = _xrecover(y)
    if x & 1 != sign:
        x = _Q - x
    P = (x, y, 1, x * y % _Q)
    if not _is_on_curve(P):
        raise ValueError("point is not on the curve")
    return P


def _is_on_curve(P) -> bool:
    x, y, z, t = P
    return ((-x * x + y * y - z * z - _D * t * t) % _Q == 0
            and (x * y - z * t) % _Q == 0)


def _encode_int(y: int) -> bytes:
    return bytes((y >> (8 * i)) & 0xFF for i in range(32))


def _decode_int(data: bytes) -> int:
    return sum(2 ** (8 * i) * b for i, b in enumerate(data))


def _hint(data: bytes) -> int:
    return _decode_int(_sha512(data))


# --- Public API (RFC 8032 sections 5.1.5 - 5.1.7) ---------------------------

def keypair_from_seed(seed: bytes) -> tuple[bytes, bytes]:
    """Derive (public_key, expanded_secret) material from a 32-byte seed.

    Returns (public_key_bytes, secret_scalar_and_prefix) — callers should
    keep the SEED as the private key and re-derive; see KeyPair below.
    """
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    h = _sha512(seed)
    a = _decode_int(h[:32])
    a &= (1 << 254) - 8
    a |= (1 << 254)
    prefix = h[32:]
    A = _scalarmult(_B, a)
    return _encode_point(A), (a, prefix)


class KeyPair:
    """An Ed25519 identity: a 32-byte seed and its derived public key."""

    def __init__(self, seed: bytes):
        self.seed = seed
        self.public_key, (self._a, self._prefix) = keypair_from_seed(seed)

    @classmethod
    def generate(cls) -> "KeyPair":
        import secrets
        return cls(secrets.token_bytes(32))

    @property
    def public_hex(self) -> str:
        return self.public_key.hex()

    @property
    def seed_hex(self) -> str:
        return self.seed.hex()

    def sign(self, message: bytes) -> bytes:
        """RFC 8032 signing. Returns the 64-byte signature."""
        r = _hint(self._prefix + message) % _L
        R = _scalarmult(_B, r)
        enc_R = _encode_point(R)
        h = _hint(enc_R + self.public_key + message) % _L
        S = (r + h * self._a) % _L
        return enc_R + _encode_int(S)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 verification. Returns True/False (never raises on bad input
    beyond returning False for malformed encodings)."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        A = _decode_point(public_key)
        R = _decode_point(signature[:32])
    except ValueError:
        return False
    S = _decode_int(signature[32:])
    if S >= _L:
        return False
    h = _hint(signature[:32] + public_key + message) % _L
    return _point_equal(_scalarmult(_B, S), _point_add(R, _scalarmult(A, h)))


# Hex-string conveniences used at the AirShield / AirBridge boundary.

def sign_hex(keypair: KeyPair, message: str) -> str:
    return keypair.sign(message.encode("utf-8")).hex()


def verify_hex(public_hex: str, message: str, signature_hex: str) -> bool:
    try:
        pub = bytes.fromhex(public_hex)
        sig = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    return verify(pub, message.encode("utf-8"), sig)
