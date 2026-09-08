# -*- coding: utf-8 -*-
"""Encryption at rest, and file permissions that actually apply.

Two jobs, both of which the rest of Seam had been leaving to the disk:

  - **A sealed box.** ChaCha20-Poly1305, RFC 8439. Chosen over AES because it
    is add-rotate-xor arithmetic with no lookup tables: about a hundred lines
    that can be read and checked against the RFC's own test vectors, rather
    than a hand-written block cipher nobody should trust. Python is not fast at
    this - a few megabytes a second - which is fine for a backup and for a
    handful of short secrets, and it is not used anywhere on a request path.

  - **File permissions.** `os.chmod(0o600)` is a no-op on Windows: the mode
    reads back as 0o666 and the file keeps whatever the directory hands down.
    On Windows this uses icacls to strip inheritance instead.

The key derivation for a passphrase is scrypt (N=2^15), because a backup
passphrase is typed by a person and will not have 128 bits of entropy in it.
"""
import os
import sys
import json
import struct
import hashlib
import secrets
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
#: Keys for data at rest. Deliberately a separate file from `.secret`, and
#: deliberately not included in a backup: a backup that carries the key that
#: opens it is a zip file with the password written on the lid.
DATA_KEY_FILE = os.path.join(BASE, ".data_key")

SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 15, 8, 1


# --------------------------------------------------------------------------- #
#  ChaCha20 (RFC 8439 section 2.4)
# --------------------------------------------------------------------------- #
_MASK = 0xFFFFFFFF


def _rotl(v, n):
    return ((v << n) | (v >> (32 - n))) & _MASK


def _quarter(s, a, b, c, d):
    s[a] = (s[a] + s[b]) & _MASK; s[d] = _rotl(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & _MASK; s[b] = _rotl(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & _MASK; s[d] = _rotl(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & _MASK; s[b] = _rotl(s[b] ^ s[c], 7)


def _block(key, counter, nonce):
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    state += list(struct.unpack("<8I", key))
    state += [counter] + list(struct.unpack("<3I", nonce))
    work = list(state)
    for _ in range(10):                              # 20 rounds, in pairs
        _quarter(work, 0, 4, 8, 12); _quarter(work, 1, 5, 9, 13)
        _quarter(work, 2, 6, 10, 14); _quarter(work, 3, 7, 11, 15)
        _quarter(work, 0, 5, 10, 15); _quarter(work, 1, 6, 11, 12)
        _quarter(work, 2, 7, 8, 13); _quarter(work, 3, 4, 9, 14)
    return struct.pack("<16I", *[(work[i] + state[i]) & _MASK for i in range(16)])


def chacha20(key, counter, nonce, data):
    out = bytearray(len(data))
    for i in range(0, len(data), 64):
        stream = _block(key, counter + i // 64, nonce)
        chunk = data[i:i + 64]
        for j, b in enumerate(chunk):
            out[i + j] = b ^ stream[j]
    return bytes(out)


# --------------------------------------------------------------------------- #
#  Poly1305 (RFC 8439 section 2.5)
# --------------------------------------------------------------------------- #
_P1305 = (1 << 130) - 5


def poly1305(key, msg):
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:32], "little")
    acc = 0
    for i in range(0, len(msg), 16):
        chunk = msg[i:i + 16]
        n = int.from_bytes(chunk + b"\x01", "little")
        acc = ((acc + n) * r) % _P1305
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(data):
    return b"\x00" * (-len(data) % 16)


def seal(key, plaintext, aad=b"", nonce=None):
    """AEAD_CHACHA20_POLY1305. Returns nonce || ciphertext || tag."""
    nonce = nonce or secrets.token_bytes(12)
    otk = _block(key, 0, nonce)[:32]
    ct = chacha20(key, 1, nonce, plaintext)
    mac_data = (aad + _pad16(aad) + ct + _pad16(ct)
                + struct.pack("<QQ", len(aad), len(ct)))
    return nonce + ct + poly1305(otk, mac_data)


def open_box(key, blob, aad=b""):
    """The inverse. Returns None when the tag does not match - a wrong key and
    a tampered file are the same answer, deliberately."""
    if len(blob) < 28:
        return None
    nonce, ct, tag = blob[:12], blob[12:-16], blob[-16:]
    otk = _block(key, 0, nonce)[:32]
    mac_data = (aad + _pad16(aad) + ct + _pad16(ct)
                + struct.pack("<QQ", len(aad), len(ct)))
    if not secrets.compare_digest(poly1305(otk, mac_data), tag):
        return None
    return chacha20(key, 1, nonce, ct)


def derive(passphrase, salt):
    """scrypt, because a passphrase a person typed is not a key.

    `maxmem` has to be given: N=2^15 with r=8 needs 32 MiB, which is exactly
    OpenSSL's default ceiling, so the call fails on the boundary without it.
    """
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt,
                          n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
                          maxmem=64 * 1024 * 1024)


# --------------------------------------------------------------------------- #
#  The instance's data key
# --------------------------------------------------------------------------- #
def data_key():
    """The key that encrypts secrets held in the database. Kept beside the
    application rather than inside the database, so that a copy of the database
    - or a backup of it - is not enough to open them."""
    env = os.environ.get("SEAM_DATA_KEY")
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()
    if os.path.exists(DATA_KEY_FILE):
        with open(DATA_KEY_FILE, "rb") as f:
            raw = f.read().strip()
        if len(raw) == 64:
            return bytes.fromhex(raw.decode("ascii"))
    raw = secrets.token_bytes(32)
    with open(DATA_KEY_FILE, "w") as f:
        f.write(raw.hex())
    secure_file(DATA_KEY_FILE)
    return raw


def encrypt_field(value):
    """A short secret on its way into the database. The marker keeps migration
    simple: anything without it is a value written before this existed."""
    if value is None:
        return None
    blob = seal(data_key(), str(value).encode("utf-8"))
    return "enc:" + blob.hex()


def decrypt_field(value):
    if not value:
        return value
    if not str(value).startswith("enc:"):
        return value                                 # written before encryption
    try:
        out = open_box(data_key(), bytes.fromhex(str(value)[4:]))
    except ValueError:
        return None
    return out.decode("utf-8") if out is not None else None


# --------------------------------------------------------------------------- #
#  File permissions
# --------------------------------------------------------------------------- #
def secure_file(path):
    """Make a credential file readable by its owner only.

    On POSIX that is one chmod. On Windows chmod does nothing useful - the mode
    reads back as 0o666 - so inheritance is stripped with icacls and the access
    list replaced with the current user and SYSTEM. An administrator can still
    take ownership; that is true of any file on the machine and pretending
    otherwise would be theatre.
    """
    if not os.path.exists(path):
        return False
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return (os.stat(path).st_mode & 0o777) == 0o600
    user = os.environ.get("USERNAME") or ""
    if not user:
        return False
    try:
        subprocess.run(["icacls", path, "/inheritance:r",
                        "/grant:r", "%s:F" % user, "/grant:r", "SYSTEM:F"],
                       check=True, capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def is_secured(path):
    """Whether `path` is closed to everyone but its owner. Reported by the
    go-live checks rather than assumed."""
    if not os.path.exists(path):
        return None
    if os.name != "nt":
        return (os.stat(path).st_mode & 0o077) == 0
    try:
        out = subprocess.run(["icacls", path], capture_output=True, timeout=15,
                             text=True, errors="ignore").stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # An inherited entry is written "(I)". None of them means inheritance was
    # stripped, which is what secure_file() does.
    return "(I)" not in out and "Everyone" not in out and "Users:" not in out


# --------------------------------------------------------------------------- #
#  Self-test
# --------------------------------------------------------------------------- #
def self_test():
    """RFC 8439's own vectors: the ChaCha20 block function (section 2.3.2),
    Poly1305 (2.5.2) and the AEAD construction (2.8.2)."""
    out = {}

    key = bytes(range(32))
    nonce = bytes.fromhex("000000090000004a00000000")
    blk = _block(key, 1, nonce)
    out["chacha20_block"] = blk.hex().startswith("10f1e7e4d13b5915500fdd1fa32071c4")

    pkey = bytes.fromhex("85d6be7857556d337f4452fe42d506a8"
                         "0103808afb0db2fd4abff6af4149f51b")
    mac = poly1305(pkey, b"Cryptographic Forum Research Group")
    out["poly1305"] = mac.hex() == "a8061dc1305136c6c22b8baf0c0127a9"

    akey = bytes.fromhex("808182838485868788898a8b8c8d8e8f"
                         "909192939495969798999a9b9c9d9e9f")
    anonce = bytes.fromhex("070000004041424344454647")
    aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
    plain = (b"Ladies and Gentlemen of the class of '99: If I could offer you "
             b"only one tip for the future, sunscreen would be it.")
    box = seal(akey, plain, aad, nonce=anonce)
    ct, tag = box[12:-16], box[-16:]
    out["aead_ciphertext"] = ct.hex().startswith("d31a8d34648e60db7b86afbc53ef7ec2")
    out["aead_tag"] = tag.hex() == "1ae10b594f09e26a7e902ecbd0600691"

    # Round trip, and the two ways it must fail.
    k = secrets.token_bytes(32)
    sealed = seal(k, b"secret", b"ctx")
    out["round_trip"] = open_box(k, sealed, b"ctx") == b"secret"
    out["rejects_wrong_key"] = open_box(secrets.token_bytes(32), sealed, b"ctx") is None
    torn = bytearray(sealed); torn[20] ^= 1
    out["rejects_tampered"] = open_box(k, bytes(torn), b"ctx") is None
    out["rejects_wrong_aad"] = open_box(k, sealed, b"other") is None

    # The passphrase path, which is only used by an encrypted backup and would
    # otherwise sit untested until the day someone needed it.
    salt = secrets.token_bytes(16)
    dk = derive("a passphrase", salt)
    out["derive_len"] = len(dk) == 32
    out["derive_is_stable"] = derive("a passphrase", salt) == dk
    out["derive_salts_apart"] = derive("a passphrase", secrets.token_bytes(16)) != dk

    out["ok"] = all(v for k_, v in out.items() if k_ != "ok")
    return out


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=1))
    sys.exit(0 if self_test()["ok"] else 1)
