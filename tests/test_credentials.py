"""Private token-file loading and startup selection without network access.

All token strings below are deterministic fake credentials. The narrow B105/B106
annotations identify fixture comparisons and configuration inputs, never secrets.
"""

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from inverter_control import config, credentials


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "ha.token"
    path.write_bytes(b"fixture.header.signature\n")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("mode", [0o400, 0o600])
@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_private_file_read(token_file, mode, ending):
    token_file.write_bytes(b"fixture.header.signature" + ending)
    token_file.chmod(mode)
    assert credentials.read_bearer_token(str(token_file)) == "fixture.header.signature"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o700, 0o4600])
def test_insecure_mode_rejected(token_file, mode):
    token_file.chmod(mode)
    with pytest.raises(ValueError, match="mode 0400 or 0600"):
        credentials.read_bearer_token(str(token_file))


def test_other_owner_rejected(token_file, monkeypatch):
    original = os.fstat

    def other_owner(descriptor):
        metadata = original(descriptor)
        return SimpleNamespace(st_mode=metadata.st_mode, st_uid=os.geteuid() + 1)

    monkeypatch.setattr(credentials.os, "fstat", other_owner)
    with pytest.raises(ValueError, match="belong to the service user"):
        credentials.read_bearer_token(str(token_file))


def test_symlink_rejected(token_file, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(token_file)
    with pytest.raises(ValueError, match="cannot be opened"):
        credentials.read_bearer_token(str(link))


def test_fifo_rejected_without_waiting_for_writer(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match="regular file"):
        credentials.read_bearer_token(str(fifo))


def test_directory_rejected(tmp_path):
    with pytest.raises(ValueError, match="regular file"):
        credentials.read_bearer_token(str(tmp_path))


@pytest.mark.parametrize("path", ["relative.token", "", 123, None])
def test_absolute_path_required(path):
    with pytest.raises(ValueError, match="absolute file path"):
        credentials.read_bearer_token(path)


@pytest.mark.parametrize(
    "value", [b"", b"\n", b"two\nlines", b"token\n\n", b"space token", b"secret\x00", b"\xff"]
)
def test_invalid_token_rejected_without_disclosure(token_file, value):
    token_file.write_bytes(value)
    with pytest.raises(ValueError, match="one nonempty bearer token") as error:
        credentials.read_bearer_token(str(token_file))
    assert str(error.value) == "HA_TOKEN_FILE must contain one nonempty bearer token"


def test_file_size_limit(token_file):
    token_file.write_bytes(b"a" * credentials.MAX_TOKEN_BYTES)
    assert len(credentials.read_bearer_token(str(token_file))) == credentials.MAX_TOKEN_BYTES
    token_file.write_bytes(b"a" * (credentials.MAX_TOKEN_BYTES + 1))
    with pytest.raises(ValueError, match="limit"):
        credentials.read_bearer_token(str(token_file))


def load_config(monkeypatch, **values):
    local = ModuleType("local_config")
    local.HA_URL = "https://ha.example.invalid"
    local.HA_SENSORS = {}
    local.VUE_SENSORS = {}
    local.HA_DUMP_LOADS = []
    for key, value in values.items():
        setattr(local, key, value)
    monkeypatch.setitem(sys.modules, "local_config", local)
    spec = importlib.util.spec_from_file_location("_credential_config_test", config.__file__)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_file_alone_enables_ha_and_preserves_connection_config(monkeypatch, token_file):
    loaded = load_config(monkeypatch, HA_TOKEN_FILE=str(token_file))
    assert loaded.HA_TOKEN == "fixture.header.signature"  # nosec B105
    assert loaded.HA_URL == "https://ha.example.invalid"
    assert loaded.ENABLE_HA


def test_selected_file_overrides_inline_and_does_not_fall_back(monkeypatch, token_file):
    loaded = load_config(  # nosec B106
        monkeypatch, HA_TOKEN_FILE=str(token_file), HA_TOKEN="old-inline"
    )
    assert loaded.HA_TOKEN == "fixture.header.signature"  # nosec B105
    token_file.unlink()
    with pytest.raises(ValueError, match="cannot be opened"):
        load_config(  # nosec B106
            monkeypatch, HA_TOKEN_FILE=str(token_file), HA_TOKEN="old-inline"
        )


def test_restarting_config_picks_up_atomic_replacement(monkeypatch, token_file):
    first = load_config(monkeypatch, HA_TOKEN_FILE=str(token_file))
    replacement = token_file.with_suffix(".new")
    replacement.write_bytes(b"replacement.header.signature")
    replacement.chmod(0o600)
    replacement.replace(token_file)
    assert first.HA_TOKEN == "fixture.header.signature"  # nosec B105
    assert (
        load_config(monkeypatch, HA_TOKEN_FILE=str(token_file)).HA_TOKEN
        == "replacement.header.signature"  # nosec B105
    )


def test_inline_token_remains_supported(monkeypatch):
    assert (
        load_config(monkeypatch, HA_TOKEN="legacy-inline").HA_TOKEN == "legacy-inline"  # nosec B105, B106
    )


@pytest.mark.parametrize("value", ["", "your_token_here", "your_long_lived_access_token_here"])
def test_placeholders_disable_ha(monkeypatch, value):
    assert not load_config(monkeypatch, HA_TOKEN=value).ENABLE_HA


def test_non_string_file_setting_rejected(monkeypatch):
    with pytest.raises(TypeError, match="must be a string"):
        load_config(monkeypatch, HA_TOKEN_FILE=Path("unused"))
