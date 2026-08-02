"""Cryptography used by the Baichuan protocol.

Two schemes are in play:

* **BC ("baichuan") encryption** - a trivial XOR against a fixed 8-byte key,
  offset by the channel id. Used for the legacy nonce handshake and the login
  message (before an AES key exists).
* **AES-128-CFB128** - used for every message body after login. The key is
  derived from the login nonce and the password; the IV is a fixed ASCII string.

Only *small XML bodies* are ever encrypted - the video payload itself is sent in
the clear - so the pure-Python AES fallback below is never in a hot path. If
``cryptography`` or ``pycryptodome`` happens to be installed we use it, but
neither is required.
"""

from __future__ import annotations

from hashlib import md5

__all__ = [
    "XML_KEY",
    "AES_IV",
    "bc_crypt",
    "md5_str_modern",
    "derive_aes_key",
    "aes_cfb_encrypt",
    "aes_cfb_decrypt",
    "AES_BACKEND",
]

# Fixed XOR key used by the "baichuan" encryption scheme.
XML_KEY = (0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF)

# Fixed AES IV, ASCII "0123456789abcdef".
AES_IV = b"0123456789abcdef"

BLOCK = 16


# --------------------------------------------------------------------------- #
# Baichuan XOR
# --------------------------------------------------------------------------- #


def bc_crypt(buf: bytes, offset: int) -> bytes:
    """XOR *buf* with the baichuan key. The operation is its own inverse.

    *offset* is the channel id from the message header (0-255).
    """
    offset &= 0xFF
    return bytes(
        byte ^ XML_KEY[(offset + idx) % 8] ^ offset for idx, byte in enumerate(buf)
    )


def md5_str_modern(text: str) -> str:
    """MD5 as the Baichuan protocol wants it: hex digest, truncated to 31
    characters (not 32 - that is not a typo), upper-cased."""
    return md5(text.encode("utf8")).hexdigest()[0:31].upper()


def derive_aes_key(nonce: str, password: str) -> bytes:
    """Derive the AES-128 session key from the login nonce and password."""
    return md5_str_modern("{}-{}".format(nonce, password))[0:16].encode("utf8")


# --------------------------------------------------------------------------- #
# AES-128 - pure Python fallback (encrypt direction only)
#
# CFB mode only ever runs the block cipher forwards, for both encryption and
# decryption, so the inverse cipher is not needed.
# --------------------------------------------------------------------------- #


def _rotl8(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (8 - shift))) & 0xFF


def _build_sbox() -> bytes:
    """Generate the AES S-box from its algebraic definition."""
    sbox = [0] * 256
    p = q = 1
    while True:
        # p *= 3 in GF(2^8)
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        # q /= 3 in GF(2^8)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        sbox[p] = (
            q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4) ^ 0x63
        ) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return bytes(sbox)


_SBOX = _build_sbox()
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(value: int) -> int:
    value <<= 1
    if value & 0x100:
        value = (value ^ 0x1B) & 0xFF
    return value


def _expand_key(key: bytes) -> list:
    """AES-128 key schedule -> 11 round keys of 16 bytes each."""
    if len(key) != 16:
        raise ValueError("only AES-128 is supported, got a {}-byte key".format(len(key)))
    words = [list(key[i * 4 : i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]  # RotWord
            temp = [_SBOX[b] for b in temp]  # SubWord
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return [
        bytes(b for word in words[r * 4 : r * 4 + 4] for b in word) for r in range(11)
    ]


def _encrypt_block(block: bytes, round_keys: list) -> bytes:
    state = [block[i] ^ round_keys[0][i] for i in range(16)]

    for rnd in range(1, 11):
        # SubBytes
        state = [_SBOX[b] for b in state]

        # ShiftRows (state is column-major: index = col * 4 + row)
        state = [
            state[0], state[5], state[10], state[15],
            state[4], state[9], state[14], state[3],
            state[8], state[13], state[2], state[7],
            state[12], state[1], state[6], state[11],
        ]  # fmt: skip

        # MixColumns (skipped in the final round)
        if rnd != 10:
            mixed = []
            for col in range(4):
                a0, a1, a2, a3 = state[col * 4 : col * 4 + 4]
                mixed.append(_xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3)
                mixed.append(a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3)
                mixed.append(a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3))
                mixed.append((_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3))
            state = mixed

        # AddRoundKey
        rk = round_keys[rnd]
        state = [state[i] ^ rk[i] for i in range(16)]

    return bytes(state)


def _py_cfb(data: bytes, key: bytes, iv: bytes, encrypt: bool) -> bytes:
    """AES-128-CFB with a full 128-bit segment size."""
    round_keys = _expand_key(key)
    out = bytearray()
    feedback = iv
    for pos in range(0, len(data), BLOCK):
        chunk = data[pos : pos + BLOCK]
        keystream = _encrypt_block(feedback, round_keys)
        result = bytes(c ^ k for c, k in zip(chunk, keystream))
        out += result
        # The feedback register always takes the *ciphertext*.
        feedback = result if encrypt else chunk
        if len(chunk) < BLOCK:  # final partial block
            break
    return bytes(out)


# --------------------------------------------------------------------------- #
# Optional accelerated backends
# --------------------------------------------------------------------------- #


def _load_backend():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def _enc(data, key, iv):
            c = Cipher(algorithms.AES(key), modes.CFB(iv)).encryptor()
            return c.update(data) + c.finalize()

        def _dec(data, key, iv):
            c = Cipher(algorithms.AES(key), modes.CFB(iv)).decryptor()
            return c.update(data) + c.finalize()

        return "cryptography", _enc, _dec
    except Exception:
        pass

    try:
        from Crypto.Cipher import AES as _AES

        def _enc(data, key, iv):
            return _AES.new(key=key, mode=_AES.MODE_CFB, iv=iv, segment_size=128).encrypt(data)

        def _dec(data, key, iv):
            return _AES.new(key=key, mode=_AES.MODE_CFB, iv=iv, segment_size=128).decrypt(data)

        return "pycryptodome", _enc, _dec
    except Exception:
        pass

    return (
        "pure-python",
        lambda data, key, iv: _py_cfb(data, key, iv, True),
        lambda data, key, iv: _py_cfb(data, key, iv, False),
    )


AES_BACKEND, _aes_enc, _aes_dec = _load_backend()


def aes_cfb_encrypt(data: bytes, key: bytes, iv: bytes = AES_IV) -> bytes:
    if not data:
        return b""
    return _aes_enc(data, key, iv)


def aes_cfb_decrypt(data: bytes, key: bytes, iv: bytes = AES_IV) -> bytes:
    if not data:
        return b""
    return _aes_dec(data, key, iv)


# --------------------------------------------------------------------------- #
# Self-test - FIPS-197 vector plus a round-trip through every backend.
# --------------------------------------------------------------------------- #


def _self_test() -> None:
    key = bytes(range(16))
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    expect = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    got = _encrypt_block(plain, _expand_key(key))
    assert got == expect, "AES block cipher self-test failed: {}".format(got.hex())

    sample = b"<?xml version='1.0'?><body></body>xyz"
    assert _py_cfb(_py_cfb(sample, key, AES_IV, True), key, AES_IV, False) == sample
    assert aes_cfb_decrypt(aes_cfb_encrypt(sample, key), key) == sample
    # The active backend must agree with the reference implementation.
    assert aes_cfb_encrypt(sample, key) == _py_cfb(sample, key, AES_IV, True)

    assert bc_crypt(bc_crypt(sample, 250), 250) == sample
    assert md5_str_modern("") == "D41D8CD98F00B204E9800998ECF8427"


if __name__ == "__main__":
    _self_test()
    print("crypto self-test OK (AES backend: {})".format(AES_BACKEND))
