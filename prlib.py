#!/usr/bin/env python3
"""Pure-Python reimplementation of the Pocket Radar Smart Coach crypto.

The official app does this in a native library (libprlib.so). This module
reproduces it in pure Python so the tool needs no proprietary binary — only
`pycryptodome`. Every function below was verified byte-for-byte against the
real library (run under a CPU emulator) on random inputs.

  - encrypt(data16, key16) / decrypt(data16, key16): plain AES-128-ECB.
  - return_key(cipher24): derives the 16-byte session key from the three
    8-byte nonces the radar sends during pairing.

The key derivation runs 12 rounds of AES-128 *decrypt* of a nonce-seeded block
using a fixed 16-byte constant as the AES key, carrying bytes forward between
rounds, and finally concatenates two of the round outputs.
"""

from Crypto.Cipher import AES

# 16-byte constant embedded in the app's native library, used as the AES key
# for the session-key derivation below.
_CONST = bytes.fromhex("808cee9e8a3a5788a2d761138ee79575")


def _aes_ecb_decrypt(block16: bytes, key16: bytes) -> bytes:
    return AES.new(key16, AES.MODE_ECB).decrypt(block16)


class PrLib:
    """Drop-in crypto used by the client (mirrors the native JNI methods)."""

    def encrypt(self, data16: bytes, key16: bytes) -> bytes:
        return AES.new(key16, AES.MODE_ECB).encrypt(data16)

    def decrypt(self, data16: bytes, key16: bytes) -> bytes:
        return AES.new(key16, AES.MODE_ECB).decrypt(data16)

    def return_key(self, cipher24: bytes):
        """cipher24 = the three 8-byte nonces concatenated. Returns (rc, key16).
        rc is unused by the protocol and always 0 here."""
        if len(cipher24) != 24:
            raise ValueError("cipher must be 24 bytes (three 8-byte nonces)")
        outs = []
        for k in range(12):
            low = bytearray(cipher24[0:8] if k == 0 else outs[k - 1][0:8])
            low[7] ^= (12 - k)                       # per-round counter
            if k == 0:
                high = cipher24[16:24]
            elif k == 1:
                high = cipher24[8:16]
            else:
                high = outs[k - 2][8:16]             # carried from 2 rounds back
            outs.append(_aes_ecb_decrypt(bytes(low) + bytes(high), _CONST))
        return 0, outs[11][8:16] + outs[10][8:16]


if __name__ == "__main__":
    lib = PrLib()
    key = bytes(range(16))
    pt = bytes.fromhex("00000100000000000000000000001f00")
    print("AES round-trip ok:", lib.decrypt(lib.encrypt(pt, key), key) == pt)
    rc, sk = lib.return_key(bytes(range(24)))
    print("return_key(0..23) =", sk.hex())
