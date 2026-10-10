# Read-only D-Bus diagnostics

These tools parse saved native write timing and writer provenance records. They
do not submit inverter commands, start a controller, or infer physical actuation
from a D-Bus reply. Run them from a source checkout with Python 3.12.

## Saved timing logs

```sh
python3 -m scripts.dbus_diagnostics.send_timing_parser --input timing.log --output timing.json
python3 -m scripts.dbus_diagnostics.correlate_send --capture capture.json --timings timing.json --output correlation.json
```

Output files must not already exist. The parser accepts the legacy, anchored,
send-observer and separate write-lock schemas. Unknown fields, duplicate keys,
non-finite numbers, invalid chronology and inconsistent durations are rejected.
The correlator requires a bounded capture with before/after clock brackets,
start/finish times and sender/serial observations. Ambiguous joins are left
unmatched. Negative observer intervals are preserved; neither a future completing
nor a method reply proves that the inverter applied a setpoint.

## Writer provenance

`provenance_parser.parse_provenance(line)` validates an individual logged record.
For an existing Linux runtime, `read_provenance` can bind saved records to an
explicitly expected process and current source inventory:

```sh
python3 -m scripts.dbus_diagnostics.read_provenance < expectations.json > provenance.json
```

Expectations must provide `pid`, `process_start_ticks`, `boot_id`, `version`,
`sender` (a D-Bus unique name or null), `observed_monotonic_min/max`,
`logged_realtime_min/max`, and `runtime_hashes`. The latter maps `main.py`,
`version` and every installed `inverter_control/*.py` file to its SHA-256. The
inventory is bounded, but its count is not tied to a particular historical
release. Missing, extra or changed source files fail verification. Obtain these
expectations independently for the intended process and time window. Supply the
JSON through standard input (at most 64 KiB); the reader does not accept an
expectations filename or choose which file the caller supplies.

The default roots are `/data/inverter-control`, `/var/log/inverter-control` and
`/proc`; explicit `--runtime-root`, `--log-root` and `--proc-root` override them.
The reader checks source/process identity before and after reading, joins only
adjacent rotated log fragments, and queries only the selected sender's PID using
a subprocess with a two-second timeout. It never signals the controller or
changes its files, configuration or logs. Run it only on the intended Linux
host, after preparing fresh expectations. The offline parsers need no D-Bus.

Current disk hashes are not proof of loaded machine code. Missing provenance is
reported as unavailable, and multiple distinct matching records as ambiguous.
The byte/count bounds do not impose a filesystem I/O deadline. Fixture records
use synthetic identities and paths; tests also consume the current producer
directly using a socket-free writer.
