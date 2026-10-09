# Quick start

## Explore the controller without equipment

Use a workstation with Git and [uv](https://docs.astral.sh/uv/getting-started/installation/). This first exercise runs the controller's calculation tests with synthetic inputs and does not start the daemon or contact an inverter.

```bash
git clone https://github.com/victron-venus/inverter-control.git
cd inverter-control
uv python install "$(cat .python-version)"
uv sync --locked --all-extras
uv run --locked pytest tests/test_logic.py --no-cov -q
```

A successful run ends with a pytest `passed` summary. Read [LOGIC.md](../LOGIC.md) alongside [tests/test_logic.py](../tests/test_logic.py) to see how input power, limits, and previous state determine a requested setpoint. Change a synthetic test input on a development branch and rerun the test to explore the behavior. No private `local_config.py`, MQTT broker, device credentials, or D-Bus service is needed for these tests.

The deliberately focused command disables whole-project coverage reporting; before proposing a change, run the full gate in the [development guide](development.md). Passing a calculation test does not validate an installation.

## Install on an intended Venus OS device

Use the [README's requirements, configuration, and installation steps](../README.md#runtime-requirements). Select a compatible released archive, verify it, provision the private configuration, and arrange the operator-controlled commissioning window before starting the service. The [build and installation guide](build-and-install.md) documents development and packaging conventions; [signature verification](release-signatures.md) covers published artifact authentication. The [operations guide](venus-os-operations.md) explains status, upgrade, and recovery.

Do not run the live daemon on a device merely to try this quick start. Its default configuration permits hardware control, and `--dry-run` is not an electrical lockout. The synthetic exercise above is the equipment-free path.
