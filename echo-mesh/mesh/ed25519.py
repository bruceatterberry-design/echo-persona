"""Ed25519 signatures (RFC 8032), pure Python, standard library only, so a node needs no extra package to verify a signed brain update.
Slow (tens of milliseconds) but only ever used on small manifests. Reference algorithm; verified against the RFC test vectors in tests/test_brainpkg.py."""
import hashlib

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
I = pow(2, (P - 1) // 4, P)


def _h(m):
    return hashlib.sha512(m).digest()


def _inv(x):
    return pow(x, P - 2, P)


def _xrecover(y):
    xx = (y * y - 1) * _inv(D * y * y + 1)
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P:
        x = x * I % P
    return P - x if x % 2 else x


BY = 4 * _inv(5) % P
B = (_xrecover(BY), BY, 1, _xrecover(BY) * BY % P)


def _add(p, q):
    a, b = (p[1] - p[0]) * (q[1] - q[0]) % P, (p[1] + p[0]) * (q[1] + q[0]) % P
    c, d = 2 * p[3] * q[3] * D % P, 2 * p[2] * q[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _enc(p):
    zi = _inv(p[2]); x, y = p[0] * zi % P, p[1] * zi % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _dec(s):
    if len(s) != 32:
        raise ValueError("bad point")
    y = int.from_bytes(s, "little"); sign = y >> 255; y &= (1 << 255) - 1
    if y >= P:
        raise ValueError("bad point")
    x = _xrecover(y)
    if x & 1 != sign:
        x = P - x
    pt = (x, y, 1, x * y % P)
    if (-pt[0] * pt[0] + pt[1] * pt[1] - 1 - D * pt[0] ** 2 * pt[1] ** 2) % P:
        raise ValueError("not on curve")
    return pt


def _clamp(h):
    a = int.from_bytes(h[:32], "little")
    return (a & ~7 & ((1 << 254) - 1)) | (1 << 254)


def public_key(seed):
    return _enc(_mul(_clamp(_h(seed)), B))


def sign(seed, msg):
    h = _h(seed); a = _clamp(h); pk = _enc(_mul(a, B))
    r = int.from_bytes(_h(h[32:] + msg), "little") % L
    R = _enc(_mul(r, B))
    k = int.from_bytes(_h(R + pk + msg), "little") % L
    return R + ((r + k * a) % L).to_bytes(32, "little")


def verify(pk, msg, sig):
    try:
        if len(sig) != 64 or len(pk) != 32:
            return False
        A, R = _dec(pk), _dec(sig[:32])
        s = int.from_bytes(sig[32:], "little")
        if s >= L:
            return False
        k = int.from_bytes(_h(sig[:32] + pk + msg), "little") % L
        lhs, rhs = _mul(s, B), _add(R, _mul(k, A))
        return _enc(lhs) == _enc(rhs)
    except Exception:
        return False
