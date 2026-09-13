"""Run the pinned MQTT/D-Bus mock contracts on an isolated local Docker stack."""

import os
import subprocess
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

INTEGRATION_REVISION = "dc18d221c287d5f39513264d427b84b97a7db6c9"


def main() -> None:
    """Test shared mock contracts; this does not replace physical controller acceptance."""
    host = subprocess.check_output(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], text=True
    ).strip()
    if not os.environ.get("DOCKER_HOST", host).startswith(("unix://", "npipe://")):
        raise SystemExit("Integration tests require a local Docker endpoint")
    with TemporaryDirectory(prefix="controller-contracts-") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            [
                "git",
                "fetch",
                "--depth",
                "1",
                "https://github.com/victron-venus/integration-tests.git",
                INTEGRATION_REVISION,
            ],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=root, check=True)
        config = yaml.safe_load((root / "docker-compose.yml").read_text())
        overrides = yaml.safe_load((root / "docker-compose.test.yml").read_text())
        mocks = ["mqtt-broker", "mock-dbus", "mock-battery", "mock-pv"]
        config["services"] = {name: config["services"][name] for name in mocks}
        config["services"]["test-runner"] = overrides["services"]["test-runner"]
        for service in config["services"].values():
            service.pop("container_name", None)
            service.pop("ports", None)
        path = root / "compose.ci.yml"
        path.write_text(yaml.safe_dump(config, sort_keys=False))
        command = [
            "docker",
            "compose",
            "-p",
            "ci-control-" + uuid.uuid4().hex[:10],
            "-f",
            str(path),
        ]
        try:
            subprocess.run([*command, "up", "-d", "--build", *mocks], check=True)
            subprocess.run([*command, "build", "test-runner"], check=True)
            readiness = """import socket,time
deadline=time.monotonic()+90
while True:
    try:
        socket.create_connection(('mqtt-broker',1883),timeout=2).close()
        break
    except OSError:
        if time.monotonic() >= deadline:
            raise SystemExit('MQTT broker never became ready')
        time.sleep(1)
"""
            subprocess.run(
                [*command, "run", "--rm", "--no-deps", "test-runner", "python", "-c", readiness],
                check=True,
            )
            subprocess.run(
                [
                    *command,
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    "CI_REQUIRE_SERVICES=1",
                    "test-runner",
                    "python",
                    "-m",
                    "pytest",
                    "tests/integration/test_battery_pv_mocks.py",
                    "tests/integration/test_dbus_battery_pv.py",
                    "tests/integration/test_mqtt_flow.py::test_mqtt_roundtrip",
                    "-v",
                    "--tb=short",
                ],
                check=True,
            )
        finally:
            subprocess.run([*command, "logs", "--tail", "50"], check=False)
            subprocess.run([*command, "down", "--volumes", "--remove-orphans"], check=True)


if __name__ == "__main__":
    main()
