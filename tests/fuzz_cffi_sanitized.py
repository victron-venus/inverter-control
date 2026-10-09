"""Bounded deterministic CFFI API fuzzing for an ASan-instrumented native build."""

import json
import random

import cffi


def exercise(seed: int = 15293, iterations: int = 2000) -> dict[str, int]:
    # Reproducible fuzz inputs, never credentials or cryptographic randomness.
    rng = random.Random(seed)  # nosec B311
    ffi = cffi.FFI()
    ffi.cdef("struct sample { unsigned int count; unsigned char bytes[64]; };")
    malformed = ["struct {", "int[-1]", "missing_type *", "int[", "int &"]
    for _ in range(iterations):
        size = rng.randrange(1, 513)
        data = bytes(rng.randrange(256) for _ in range(size))
        owned = ffi.new("unsigned char[]", data)
        if bytes(ffi.buffer(owned, size)) != data:
            raise RuntimeError("Owned CFFI buffer changed contents")
        borrowed = bytearray(data)
        view = ffi.from_buffer("unsigned char[]", borrowed)
        offset = rng.randrange(size)
        view[offset] ^= 0xFF
        if borrowed[offset] != data[offset] ^ 0xFF:
            raise RuntimeError("Borrowed CFFI buffer changed contents")
        sample = ffi.new("struct sample *")
        sample.count = rng.randrange(2**32)
        sample.bytes[rng.randrange(64)] = rng.randrange(256)
        if ffi.sizeof(sample[0]) < 68:
            raise RuntimeError("CFFI structure has an invalid size")
        if ffi.cast("unsigned char *", owned)[offset] != data[offset]:
            raise RuntimeError("Owned pointer cast changed contents")
        try:
            ffi.typeof(rng.choice(malformed))
        except (cffi.CDefError, cffi.FFIError, TypeError, ValueError):
            pass
        else:
            raise AssertionError("Malformed declaration was unexpectedly accepted")
    return {"seed": seed, "iterations": iterations}


if __name__ == "__main__":
    print(json.dumps(exercise(), sort_keys=True))
