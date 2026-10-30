# Power limits

The default automatic setpoint range is **−2300 to +2250 W**. These limits bound
the requested power at the inverter AC input; they do not cap the total measured
grid power of the site. Negative values request export/discharge and positive
values request import/charging. Use watts, not volts.

`local_config.example.py` declares both settings explicitly:

```python
POWER_LIMIT_MIN = -2300
POWER_LIMIT_MAX = 2250
```

Choose limits appropriate for the installation. Configuration and installer
values must be integers in the supported range `-3000..3000`, with the minimum
no greater than the maximum. An entirely positive or negative range is allowed;
it can restrict which automatic operating modes are possible. Sustained
`setpoint_override` commands have a separate contract and deliberately bypass
these automatic limits; see [interfaces](interfaces.md).

## Sources and precedence

At startup the controller uses, in order:

1. A saved pair in `/data/setupOptions/inverter-control/power-limits.json`.
2. `POWER_LIMIT_MIN` and `POWER_LIMIT_MAX` in the installed `local_config.py`.
3. The built-in defaults, −2300/+2250 W, for missing local settings.

The saved file contains a complete pair, for example
`{"min": -1800, "max": 2000}`. An invalid explicit setting is an error, not a
reason to silently fall back to a different limit. Installation checks the
effective pair before stopping the service.

Ordinary updates preserve the saved pair and local configuration. An installation
without an explicit choice does not create a JSON override just to store the
defaults. To return to local configuration after saving an override, back up and
remove `power-limits.json`, then restart the controller with its operating modes
preserved. While that file exists, editing only `local_config.py` does not override
it.

## Choose limits during installation

Interactive SetupHelper installation offers the current effective values and
lets you enter different limits. Leaving the entries blank keeps the current
values and their existing configuration source. PackageManager's
`setup install auto` remains noninteractive.

For manual installation, pass both values explicitly:

```sh
POWER_LIMIT_MIN=-1800 POWER_LIMIT_MAX=2000 \
  sh /path/to/release/inverter-control/update.sh
```

The same variables work with SetupHelper:

```sh
POWER_LIMIT_MIN=-1800 POWER_LIMIT_MAX=2000 \
  bash /path/to/release/inverter-control/setup install auto
```

Direct `update.sh` also offers the wizard when run from a terminal. Use
`CONFIGURE_POWER_LIMITS=0` to keep the effective values without prompting.
`deploy.sh <SSH-host>` forwards an explicitly supplied pair after validating it
on the workstation; absent variables leave the device's choice unchanged.

These explicit choices save the complete pair in `power-limits.json`, which
survives future installations. Without the variables, installation uses the
existing configuration. For SetupHelper, provision site settings in
`/data/setupOptions/inverter-control/local_config.py`; that copy becomes the
installed configuration. For direct `update.sh`, edit the installed
`/data/inverter-control/local_config.py`. Keep both private copies consistent if
you use both installation methods.

## Change limits later

The desktop's limit controls and MQTT `inverter/cmd/limits` apply the new pair
immediately and save it for subsequent restarts:

```json
{"min": -1800, "max": 2000}
```

Publish commands without MQTT retention and confirm the effective `limits` in
`inverter/state`. The existing MQTT compatibility path clamps endpoints to
`-3000..3000`; it saves the effective pair. A persistence failure leaves the
previous running limits in place. This does not change the working-mode flags.
Add `"persist": false` to make a temporary runtime change without replacing the
saved pair. Sending only one endpoint keeps the other current endpoint; sending
both is preferable when choosing a complete range.

For configuration on disk, the standalone tool supports a wizard or explicit
values:

```sh
cd /data/inverter-control
python3 inverter_control/power_limits.py --local-config local_config.py --show
python3 inverter_control/power_limits.py --local-config local_config.py --interactive --install
# Or:
python3 inverter_control/power_limits.py --min -1800 --max 2000 --install
```

The standalone tool reads the two local settings as literal integers without
executing the private Python configuration. Disk-only edits take effect at the
next controller startup; the tool does not restart the service or change live
hardware. Preserve and restore active operating-mode flags during a manual
restart, as described in the installation and upgrade guidance.
