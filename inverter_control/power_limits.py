"""Persistent normal-control power limits and a standalone installation wizard.

This module does not import configuration, credentials, D-Bus or the controller.
The fixed SetupHelper file overrides local_config.py and survives upgrades.
"""

import argparse
import ast
import json
import os
import stat
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

DEFAULT_MIN = -2300
DEFAULT_MAX = 2250
ABSOLUTE_LIMIT = 3000
SETUP_FILE = Path("/data/setupOptions/inverter-control/power-limits.json")
LOCAL_CONFIG = Path("/data/inverter-control/local_config.py")
SETUP_CONFIG = Path("/data/setupOptions/inverter-control/local_config.py")
LEGACY_CONFIG = Path("/data/inverter-control/secrets.py")
SOURCE_ROOT = Path(__file__).resolve().parent.parent
CONFIG_SOURCES = ("installed", "setup", "source", "legacy", "example", "defaults")
MAX_BYTES = 4096


def validate_power_limits(minimum, maximum) -> dict[str, int]:
    """Accept ordered integer watts within the existing normal-control envelope."""
    if type(minimum) is not int or type(maximum) is not int:
        raise ValueError("Power limits must be integers in watts")
    if not -ABSOLUTE_LIMIT <= minimum <= maximum <= ABSOLUTE_LIMIT:
        raise ValueError("Power limits must satisfy -3000 <= min <= max <= 3000 watts")
    return {"min": minimum, "max": maximum}


def _limits_object(data) -> dict[str, int]:
    if not isinstance(data, dict) or set(data) != {"min", "max"}:
        raise ValueError('Power limits must be an object with exactly "min" and "max"')
    return validate_power_limits(data["min"], data["max"])


def read_power_limits(path: Path | None = None) -> dict[str, int] | None:
    """Return None only for an absent file; explicit invalid settings are errors."""
    path = SETUP_FILE if path is None else Path(path)
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError(f"Power limits must be a regular file: {path}")
        content = source.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError(f"Power limits file exceeds {MAX_BYTES} bytes: {path}")
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ValueError(f"Invalid power limits JSON: {path}") from exc
    try:
        return _limits_object(data)
    except ValueError as exc:
        raise ValueError(f"Invalid power limits in {path}: {exc}") from exc


def load_power_limits(minimum=DEFAULT_MIN, maximum=DEFAULT_MAX, *, path=None) -> dict[str, int]:
    selected = read_power_limits(path)
    return selected if selected is not None else validate_power_limits(minimum, maximum)


def write_power_limits(limits: dict[str, int], *, path=None) -> dict[str, int]:
    """Atomically persist a complete pair, leaving the previous file on failure."""
    limits = _limits_object(limits)
    path = SETUP_FILE if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileNotFoundError):
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError(f"Power limits must be a regular file: {path}")
    fd, temporary = tempfile.mkstemp(prefix=".power-limits-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(json.dumps(limits, sort_keys=True) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
    return limits


def config_source_path(source: str) -> Path | None:
    """Select a supported location; CLI text is never used as a filesystem path."""
    if source == "installed":
        return LOCAL_CONFIG
    if source == "setup":
        return SETUP_CONFIG
    if source == "source":
        return SOURCE_ROOT / "local_config.py"
    if source == "legacy":
        return LEGACY_CONFIG
    if source == "example":
        return SOURCE_ROOT / "local_config.example.py"
    if source == "defaults":
        return None
    raise ValueError("Unsupported configuration source")


def _local_config_tree(path: Path | None) -> ast.Module:
    if path is None:
        return ast.Module(body=[], type_ignores=[])
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    try:
        tree = ast.parse(text, filename=str(path))
        compile(tree, str(path), "exec")
    except SyntaxError as exc:
        # Do not include a source line: it may contain private configuration.
        raise ValueError(f"Invalid Python configuration in {path}, line {exc.lineno}") from exc
    return tree


def _assignment_parts(node):
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


def _literal_limit(value, name: str, path: Path | None):
    try:
        return ast.literal_eval(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a literal integer in {path}") from exc


def _local_limits_from_tree(tree: ast.Module, path: Path | None) -> dict[str, int]:
    names = {"POWER_LIMIT_MIN": DEFAULT_MIN, "POWER_LIMIT_MAX": DEFAULT_MAX}
    handled = set()
    for node in tree.body:
        targets, value = _assignment_parts(node)
        for target in targets:
            if isinstance(target, ast.Name) and target.id in names:
                names[target.id] = _literal_limit(value, target.id, path)
                handled.add(id(target))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id in names
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and id(node) not in handled
        ):
            raise ValueError(f"{node.id} must be a top-level literal assignment in {path}")
    return validate_power_limits(names["POWER_LIMIT_MIN"], names["POWER_LIMIT_MAX"])


def local_config_limits(path: Path) -> dict[str, int]:
    """Read only literal limit assignments without running a private config file."""
    return _local_limits_from_tree(_local_config_tree(path), path)


def configured_power_limits(local_config: Path | None) -> dict[str, int]:
    tree = _local_config_tree(local_config)
    selected = read_power_limits()
    return selected if selected is not None else _local_limits_from_tree(tree, local_config)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-source",
        choices=CONFIG_SOURCES,
        default="installed",
        help="Configuration location; defaults skips local configuration files",
    )
    parser.add_argument("--min", type=int, dest="minimum", help="Minimum normal setpoint, watts")
    parser.add_argument("--max", type=int, dest="maximum", help="Maximum normal setpoint, watts")
    parser.add_argument("--interactive", action="store_true", help="Prompt using current values")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true", help=f"Save to {SETUP_FILE}")
    action.add_argument("--check", action="store_true", help="Validate without changing files")
    action.add_argument("--show", action="store_true", help="Print the effective limits as JSON")
    args = parser.parse_args(argv)
    if (args.minimum is None) != (args.maximum is None):
        parser.error("--min and --max must be supplied together")
    if args.interactive and args.minimum is not None:
        parser.error("--interactive cannot be combined with --min/--max")
    if args.interactive and not args.install:
        parser.error("--interactive requires --install")
    try:
        local_config = config_source_path(args.config_source)
        if args.minimum is not None:
            _local_config_tree(local_config)
            limits = validate_power_limits(args.minimum, args.maximum)
        else:
            limits = configured_power_limits(local_config)
        save = args.install
        if args.interactive:
            print("Normal-control power limits in watts: negative = export, positive = import.")
            print("Supported range: -3000 to +3000 W; press Enter to keep the shown value.")
            minimum = input(f"Minimum [{limits['min']} W]: ").strip()
            maximum = input(f"Maximum [{limits['max']} W]: ").strip()
            limits = validate_power_limits(
                int(minimum) if minimum else limits["min"],
                int(maximum) if maximum else limits["max"],
            )
            # Keeping values must also preserve the source of those values.
            save = bool(minimum or maximum)
        if save:
            write_power_limits(limits)
        print(json.dumps(limits, sort_keys=True))
        return 0
    except (OSError, ValueError, EOFError) as exc:
        print(f"Power limits: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
