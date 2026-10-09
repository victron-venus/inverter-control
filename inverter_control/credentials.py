"""Read an operator-managed bearer token without mixing it into configuration."""

import os
import re
import stat

MAX_TOKEN_BYTES = 16384
_BEARER_TOKEN = re.compile(rb"[A-Za-z0-9._~+/-]+=*")


def read_bearer_token(path: str) -> str:
    """Read one private regular file; never include its contents in an error.

    The parent directories are an administrator-controlled trust boundary.
    Opening first with O_NOFOLLOW and then inspecting that descriptor avoids
    a check/open race on the final path. O_NONBLOCK prevents a FIFO from
    hanging startup before the regular-file check can reject it.
    """
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("HA_TOKEN_FILE must be an absolute file path")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise ValueError("HA_TOKEN_FILE cannot be opened as a private regular file") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("HA_TOKEN_FILE must be a regular file")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) not in (0o400, 0o600):
            raise ValueError("HA_TOKEN_FILE must belong to the service user with mode 0400 or 0600")
        with os.fdopen(descriptor, "rb", closefd=False) as token_file:
            raw = token_file.read(MAX_TOKEN_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_TOKEN_BYTES:
        raise ValueError("HA_TOKEN_FILE exceeds the 16384-byte limit")
    # Permit the single final line ending normally written by an editor.
    token = raw.removesuffix(b"\n").removesuffix(b"\r")
    if not _BEARER_TOKEN.fullmatch(token):
        raise ValueError("HA_TOKEN_FILE must contain one nonempty bearer token")
    return token.decode("ascii")
