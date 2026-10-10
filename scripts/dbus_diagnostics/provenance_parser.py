"""Exact, separate parser for the bounded in-process writer provenance schema."""

# Exact wire schemas reject bools/subclasses; explicit guards keep rejection paths clear.
# pylint: disable=unidiomatic-typecheck,too-many-boolean-expressions,too-many-return-statements

import ast
import math
import re

PREFIX = "Native D-Bus write timing: "
SCHEMA = "dbus-send-provenance-v1"
PAYLOAD_LIMIT = 32768
TOP = {
    "phase",
    "schema",
    "clock",
    "observed_at",
    "pid",
    "loop_native_tid",
    "sender",
    "send_returned",
    "underlying_send",
    "bus",
    "writer",
    "future",
    "modules",
    "modules_truncated",
}
HASH = re.compile(r"[0-9a-f]{64}")


def text(value, limit, optional=True):
    if value is None:
        return optional
    if type(value) is not str:
        return False
    return len(value) <= limit


def finite(value):
    try:
        return type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def positive_int(value):
    return type(value) is int and value > 0


def digest(value):
    return type(value) is str and HASH.fullmatch(value) is not None


def descriptor(value, optional=False):
    if value is None:
        return optional
    return (
        type(value) is dict
        and set(value) == {"module", "qualname"}
        and text(value["module"], 256)
        and text(value["qualname"], 256)
    )


def role(value, empty_allowed=False):
    if (
        type(value) is not dict
        or set(value) != {"class", "mro", "mro_truncated"}
        or type(value["mro"]) is not list
        or len(value["mro"]) > 8
        or type(value["mro_truncated"]) is not bool
        or (value["mro_truncated"] and len(value["mro"]) != 8)
        or not all(descriptor(item) for item in value["mro"])
    ):
        return False
    if not value["mro"]:
        return empty_allowed and value["class"] is None and not value["mro_truncated"]
    return value["class"] == value["mro"][0]


def code(value):
    if type(value) is not dict:
        return False
    if value.get("status") in {"no_python_code", "code_budget_exceeded"}:
        return set(value) == {"status"}
    return (
        set(value) == {"status", "format", "python", "sha256"}
        and value["status"] == "captured_python_code"
        and value["format"] == "python-marshal-v4-sha256"
        and type(value["python"]) is str
        and re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}", value["python"]) is not None
        and digest(value["sha256"])
    )


def module(value):
    base = {"module", "file", "spec_origin", "status"}
    if (
        type(value) is not dict
        or not base.issubset(value)
        or not text(value["module"], 256, False)
        or not text(value["file"], 512)
        or not text(value["spec_origin"], 512)
    ):
        return False
    status = value["status"]
    allowed = value["module"] in {
        "builtins",
        "_asyncio",
        "inverter_control.dbus_native",
    } or value["module"].startswith(("dbus_fast.", "asyncio."))
    if status == "module_not_allowlisted":
        return (
            not allowed
            and set(value) == base
            and value["file"] is None
            and value["spec_origin"] is None
        )
    if not allowed:
        return False
    if status == "module_not_loaded":
        return set(value) == base and value["file"] is None and value["spec_origin"] is None
    if status in {
        "no_bounded_file_path",
        "not_regular_file",
        "file_budget_exceeded",
        "file_changed_during_read",
    }:
        return set(value) == base
    if status == "file_unavailable":
        return (
            set(value) == base | {"error_type"}
            and text(value["error_type"], 256, False)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value["error_type"]) is not None
        )
    return (
        status == "current_file_hashed"
        and set(value) == base | {"bytes", "current_file_sha256", "loaded_bytes_verified"}
        and type(value["file"]) is str
        and type(value["bytes"]) is int
        and 0 <= value["bytes"] <= 4 * 1024 * 1024
        and digest(value["current_file_sha256"])
        and value["loaded_bytes_verified"] is False
    )


def validate_provenance(value):
    """Return an exact record or None. Never call a disk hash loaded-code proof."""
    try:
        if (
            type(value) is not dict
            or set(value) != TOP
            or value["phase"] != "writer_provenance"
            or value["schema"] != SCHEMA
            or value["clock"] != "monotonic"
            or not finite(value["observed_at"])
            or not positive_int(value["pid"])
            or not positive_int(value["loop_native_tid"])
            or not text(value["sender"], 128)
            or (
                value["sender"] is not None
                and re.fullmatch(r":[0-9]+\.[0-9]+", value["sender"]) is None
            )
            or type(value["send_returned"]) is not bool
            or type(value["modules_truncated"]) is not bool
        ):
            return None
        send = value["underlying_send"]
        if (
            type(send) is not dict
            or set(send) != {"module", "qualname", "bound_callable_type", "code"}
            or not text(send["module"], 256)
            or not text(send["qualname"], 256)
            or not descriptor(send["bound_callable_type"])
            or not code(send["code"])
        ):
            return None
        if not all(
            role(
                value[name],
                empty_allowed=name == "future" and not value["send_returned"],
            )
            for name in ("bus", "writer", "future")
        ):
            return None
        if not value["send_returned"] and value["future"]["mro"]:
            return None
        modules = [send["module"]] if send["module"] else []
        for name in ("bus", "writer", "future"):
            for item in value[name]["mro"]:
                if item["module"] and item["module"] not in modules:
                    modules.append(item["module"])
        rows = value["modules"]
        if (
            type(rows) is not list
            or len(rows) > 8
            or not all(module(item) for item in rows)
            or [item["module"] for item in rows] != modules[:8]
            or value["modules_truncated"] != (len(modules) > 8)
            or sum(item.get("bytes", 0) for item in rows) > 8 * 1024 * 1024
            or len(repr(value).encode("utf-8")) > PAYLOAD_LIMIT
        ):
            return None
        return value
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


def parse_provenance(line):
    """Parse only the provenance phase; native timing schemas remain independent."""
    if PREFIX not in line:
        return None
    payload = line.split(PREFIX, 1)[1].removesuffix("\n").removesuffix("\r")
    try:
        if len(payload.encode("utf-8")) > PAYLOAD_LIMIT:
            return None
        tree = ast.parse(payload.strip(), mode="eval")
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [ast.literal_eval(key) for key in node.keys]
                if not all(type(key) is str for key in keys) or len(set(keys)) != len(keys):
                    return None
        return validate_provenance(ast.literal_eval(tree))
    except (
        SyntaxError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        UnicodeError,
    ):
        return None
