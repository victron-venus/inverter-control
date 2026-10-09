"""Stage or remove the native package without touching the running controller."""

import argparse
import json
import os
import shutil
from pathlib import Path

PAYLOAD = (
    "main.py",
    "inverter_control",
    "version",
    "gitHubInfo",
    "setup",
    "update.sh",
    "setup_ssl.sh",
    "keepalive.sh",
    "local_config.example.py",
    "install.py",
    "service",
)
MANIFEST = ".installed-files.json"
PREFIX = Path("data/inverter-control")


def reject_links(path: Path) -> None:
    """Do not let a staging path redirect a write outside the selected root."""
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise ValueError(f"Staging path must not contain a symlink: {component}")
        if component.is_file() and component.stat().st_nlink > 1:
            raise ValueError(f"Staging path must not contain a hardlink: {component}")


def staged_package(destdir: str) -> Path:
    if not destdir:
        raise ValueError("DESTDIR must select a non-root staging directory")
    root = Path(os.path.abspath(destdir))
    reject_links(root)
    if root == Path(root.anchor):
        raise ValueError("DESTDIR must not be the live filesystem root")
    package = root / PREFIX
    reject_links(package)
    return package


def payload_files(source: Path) -> list[Path]:
    files = []
    for item in PAYLOAD:
        path = source / item
        if path.is_symlink():
            raise ValueError(f"Package input must not be a symlink: {path}")
        if not path.exists():
            raise ValueError(f"Missing package input: {item}")
        entries = sorted(path.rglob("*")) if path.is_dir() else [path]
        for entry in entries:
            if entry.is_symlink():
                raise ValueError(f"Package input must not be a symlink: {entry}")
            if entry.is_file() and not any(
                part == "__pycache__" or part.endswith((".pyc", ".pyo")) for part in entry.parts
            ):
                files.append(entry)
    return files


def allowed_name(name: str) -> bool:
    path = Path(name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and bool(path.parts)
        and path.parts[0] in PAYLOAD
        and (len(path.parts) == 1 or path.parts[0] in {"inverter_control", "service"})
    )


def stage(source: Path, destdir: str) -> None:
    package = staged_package(destdir)
    files = payload_files(source)
    names = [path.relative_to(source).as_posix() for path in files]
    # Validate every destination before creating or replacing any payload.
    for name in (*names, MANIFEST):
        reject_links(package / name)
    for path, name in zip(files, names, strict=True):
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
    (package / MANIFEST).write_text(json.dumps(sorted(names), indent=2) + "\n")


def unstage(destdir: str) -> None:
    package = staged_package(destdir)
    manifest = package / MANIFEST
    reject_links(manifest)
    names = json.loads(manifest.read_text())
    if not isinstance(names, list) or not all(
        isinstance(name, str) and allowed_name(name) for name in names
    ):
        raise ValueError("Invalid staged installation manifest")
    for name in names:
        reject_links(package / name)
    for name in names:
        (package / name).unlink(missing_ok=True)
    manifest.unlink()
    for directory in sorted(package.rglob("*"), reverse=True):
        if directory.is_dir() and not directory.is_symlink():
            try:
                directory.rmdir()
            except OSError:
                pass  # Preserve operator-created files and nonempty directories.
    try:
        package.rmdir()
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "uninstall"])
    parser.add_argument("--destdir", default=os.environ.get("DESTDIR", ""))
    args = parser.parse_args()
    if args.action == "install":
        stage(Path(__file__).resolve().parent, args.destdir)
    else:
        unstage(args.destdir)


if __name__ == "__main__":
    main()
