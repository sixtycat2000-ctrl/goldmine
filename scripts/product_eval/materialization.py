"""Transport declared source kinds without injecting text or changing assets.

The frozen kind, rather than questions or oracle views, establishes that a real
browser capture is a screenshot. PNG bytes do not carry the macOS capture flag.
Both evaluation arms receive the same flag and its exact native read-back proof.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
from pathlib import Path
import plistlib
import sys

from answer_eval.protocol import EvaluationError, fields

POLICY = "declared-source-kind/v1"
CAPTURE_ATTRIBUTE = "com.apple.metadata:kMDItemIsScreenCapture"
CAPTURE_VALUE = plistlib.dumps(True, fmt=plistlib.FMT_BINARY)
MAX_ATTRIBUTE_BYTES = 256


def expected_flags(sources):
    return {path: CAPTURE_VALUE.hex() if source["kind"] == "screenshot" else None
            for path, source in sources.items()}


def _native():
    if sys.platform != "darwin":
        raise EvaluationError("screen-capture metadata requires macOS and an xattr-capable filesystem", status="MISSING")
    lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    lib.getxattr.restype = ctypes.c_ssize_t
    lib.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                            ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    lib.setxattr.restype = ctypes.c_int
    lib.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                            ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    return lib


def read_flag(path):
    if sys.platform != "darwin":
        return None  # No flag is required when the source is not a screenshot.
    lib = _native()
    cpath, name = os.fsencode(path), CAPTURE_ATTRIBUTE.encode("ascii")
    size = lib.getxattr(cpath, name, None, 0, 0, 0)
    if size < 0:
        code = ctypes.get_errno()
        if code == getattr(errno, "ENOATTR", 93):
            return None
        raise EvaluationError(f"cannot read capture metadata for {Path(path).name}: errno {code}", status="MISSING")
    if not 0 < size <= MAX_ATTRIBUTE_BYTES:
        raise EvaluationError("invalid/oversized screen-capture attribute", status="MISSING")
    buf = ctypes.create_string_buffer(size)
    if lib.getxattr(cpath, name, buf, size, 0, 0) != size:
        raise EvaluationError("capture metadata changed during read-back", status="MISSING")
    return bytes(buf.raw).hex()


def materialize(path, source):
    if source["kind"] == "screenshot":
        lib = _native()
        value = ctypes.create_string_buffer(CAPTURE_VALUE)
        if lib.setxattr(os.fsencode(path), CAPTURE_ATTRIBUTE.encode("ascii"),
                        value, len(CAPTURE_VALUE), 0, 0) != 0:
            raise EvaluationError(f"cannot materialize screenshot metadata: errno {ctypes.get_errno()}", status="MISSING")
    actual = read_flag(path)
    expected = CAPTURE_VALUE.hex() if source["kind"] == "screenshot" else None
    if actual != expected:
        raise EvaluationError("declared source kind and actual capture metadata differ", status="MISSING")
    return actual


def validate(corpus, sources, receipt):
    fields(receipt, {"policy", "expected", "observed"}, "source materialization")
    expected = expected_flags(sources)
    if receipt["policy"] != POLICY or receipt["expected"] != expected or receipt["observed"] != expected:
        raise EvaluationError("build source-kind materialization proof differs from the frozen source kinds", status="MISSING")
    for path, source in sources.items():
        if source["kind"] == "screenshot" and sys.platform != "darwin":
            _native()  # Fail closed; byte-only copies cannot silently skip OCR.
        if read_flag(Path(corpus) / path) != expected[path]:
            raise EvaluationError("capture metadata was lost/changed; rebuild both evaluation inputs explicitly", status="MISSING")
