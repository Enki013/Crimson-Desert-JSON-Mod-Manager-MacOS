"""
Pearl Abyss checksum — Jenkins Lookup3 variant with PA_MAGIC = 0x2145E233.

Used for:
  - 0.papgt  FileCrc  (bytes 4-7)  = pa_checksum(data[12:])
  - 0.pamt   HeaderCrc (bytes 0-3) = pa_checksum(data[12:])
"""

import struct

PA_MAGIC = 0x2145E233
_MASK = 0xFFFFFFFF


def _rotl(v, n):
    v &= _MASK
    return ((v << n) | (v >> (32 - n))) & _MASK


def _rotr(v, n):
    v &= _MASK
    return ((v >> n) | (v << (32 - n))) & _MASK


def pa_checksum(data: bytes) -> int:
    """Return the 32-bit PA Jenkins Lookup3 hash of *data*."""
    length = len(data)
    if length == 0:
        return 0

    a = b = c = (length - PA_MAGIC) & _MASK

    offset = 0
    remaining = length

    while remaining > 12:
        a = (a + struct.unpack_from('<I', data, offset)[0]) & _MASK
        b = (b + struct.unpack_from('<I', data, offset + 4)[0]) & _MASK
        c = (c + struct.unpack_from('<I', data, offset + 8)[0]) & _MASK

        a = (a - c) & _MASK; a ^= _rotl(c, 4);  c = (c + b) & _MASK
        b = (b - a) & _MASK; b ^= _rotl(a, 6);  a = (a + c) & _MASK
        c = (c - b) & _MASK; c ^= _rotl(b, 8);  b = (b + a) & _MASK
        a = (a - c) & _MASK; a ^= _rotl(c, 16); c = (c + b) & _MASK
        b = (b - a) & _MASK; b ^= _rotl(a, 19); a = (a + c) & _MASK
        c = (c - b) & _MASK; c ^= _rotl(b, 4);  b = (b + a) & _MASK

        offset += 12
        remaining -= 12

    # Tail (fall-through switch)
    if remaining >= 12: c = (c + (data[offset + 11] << 24)) & _MASK
    if remaining >= 11: c = (c + (data[offset + 10] << 16)) & _MASK
    if remaining >= 10: c = (c + (data[offset + 9] << 8)) & _MASK
    if remaining >= 9:  c = (c + data[offset + 8]) & _MASK
    if remaining >= 8:  b = (b + (data[offset + 7] << 24)) & _MASK
    if remaining >= 7:  b = (b + (data[offset + 6] << 16)) & _MASK
    if remaining >= 6:  b = (b + (data[offset + 5] << 8)) & _MASK
    if remaining >= 5:  b = (b + data[offset + 4]) & _MASK
    if remaining >= 4:  a = (a + (data[offset + 3] << 24)) & _MASK
    if remaining >= 3:  a = (a + (data[offset + 2] << 16)) & _MASK
    if remaining >= 2:  a = (a + (data[offset + 1] << 8)) & _MASK
    if remaining >= 1:  a = (a + data[offset]) & _MASK

    # Finalization (PA modified Jenkins final mix)
    v82 = ((b ^ c) - _rotl(b, 14)) & _MASK
    v83 = ((a ^ v82) - _rotl(v82, 11)) & _MASK
    v84 = ((v83 ^ b) - _rotr(v83, 7)) & _MASK
    v85 = ((v84 ^ v82) - _rotl(v84, 16)) & _MASK
    v86 = _rotl(v85, 4)
    t   = ((v83 ^ v85) - v86) & _MASK
    v87 = ((t ^ v84) - _rotl(t, 14)) & _MASK

    return ((v87 ^ v85) - _rotr(v87, 8)) & _MASK
