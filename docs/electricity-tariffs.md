# Controller-owned electricity tariffs

Electricity tariffs describe dashboard energy prices and billing periods. They
are optional and independent of the controller's expensive-hours settings,
battery charging policy and inverter setpoints. No price is assumed when a
plan is missing. Emporia is one possible source; manual tariffs work without
an Emporia account or credentials.

The controller owns the installation's tariff. Compatible dashboard editors
save to the controller and use the plan it acknowledges; each desktop does not
need its own tariff configuration. Updating prices does not change expensive
hours, control flags, battery charging policy or live inverter setpoints.

## What to enter

Supply a name, three-letter currency, IANA time zone, energy prices per kWh,
optional seasonal calendar months, and an optional billing period start day.
For example, **22.56 cents/kWh is 0.2256 USD/kWh**. Billing day **17** means a
period from the 17th through the 16th of the following month, not a payment
due date. Days 29–31 use the last day of shorter months. Seasons change on the
first of their calendar months in the tariff time zone, including daylight
saving time, independently of the billing boundary.

Prices have half-hour resolution and can differ by weekday. All seven days
must be covered, with no gaps or overlaps; zero and negative prices are valid.
Split overnight periods at midnight. This model covers energy charges only;
it does not model holidays, tiers, demand charges, fixed fees, taxes or export
credits. A billing period is displayed, but a time-of-use invoice total needs
interval consumption data and is not inferred from daily kWh alone.
Updated web and desktop dashboards accept measured grid-import CSV/JSON through
**Interval energy cost**. See the [dashboard import guide](https://github.com/victron-venus/inverter-dashboard-vue/blob/main/docs/electricity-tariffs.md#measured-interval-energy-cost).
The controller currently has no measured grid-energy history source and publishes
`daily_stats.grid_kwh: null` rather than a fabricated zero. This does not affect
instantaneous grid telemetry or control.

## SetupHelper / PackageManager

The persistent operator file is:

```text
/data/setupOptions/inverter-control/electricity-tariff.json
```

When running `setup` interactively and choosing Install, answer **y** to
“Configure electricity prices, seasons and billing day now?” The wizard asks
for the metadata, default price periods and each optional season. Enter one
period per line, then a blank line to finish that schedule. For example:

```text
00:00 15:00 0.20
15:00 16:00 0.30
16:00 21:00 0.40
21:00 24:00 0.30
```

A fourth column can restrict a period to weekdays, such as `1,2,3,4,5`; Monday
is 1 and Sunday is 7. Omitting it applies the period to all seven days. Add
separate periods for remaining days. Enter season months as `6,7,8,9`, and a
blank season name when finished. Blank billing day means unknown.

The entire plan is validated before the wizard atomically replaces the file.
Invalid input, cancellation or end-of-input leaves the previous tariff intact.
Answering **n** or leaving the setup question blank preserves the current file.
`setup install auto` and PackageManager background installs **never prompt**;
they validate an existing tariff and retain it across updates and reinstalls.
Invalid tariff files stop setup before configuration replacement or service
restart. Package removal also retains this operator-owned file.
An explicitly cleared plan is stored as JSON `null`; automatic installations
preserve this setting too, so a fallback file cannot resurrect a cleared plan.

To configure an already installed system directly:

```sh
cd /data/inverter-control
python3 inverter_control/tariff.py --interactive --install
svc -t /service/inverter-control
```

Restart the controller after changing the file manually; clients receive the new plan
through its existing `ui_config.electricity_tariff` state field. The tariff is
read at startup, not watched continuously.

## Editing from a dashboard

Compatible editors read `ui_config.electricity_tariff` and
`ui_config.electricity_tariff_status` from `inverter/state`. Status contains
`writable`, the current `revision`, the latest `request_id`, and `error`.
The status is present before the first telemetry sweep and in slim state.

An editor sends a non-retained JSON command to
`inverter/cmd/electricity_tariff` (or the configured MQTT prefix):

```json
{
  "request_id": "editor-unique-save-id",
  "revision": "the-64-character-revision-from-controller-state",
  "plan": null
}
```

`plan` is the complete tariff object or `null` to clear it. `request_id` must
contain 1–128 ASCII letters, digits, underscores, dots, colons or hyphens.
The revision is an opaque concurrency token, not an authentication credential;
commands use the installation's existing MQTT access controls. No extra command
fields are accepted. Retained commands and payloads over 100 KB are ignored
before parsing, so reconnect cannot replay an old edit.

The controller validates and atomically saves the plan to the persistent
SetupHelper file before publishing a successful acknowledgement. Editors should
wait for the matching `request_id` and an empty `error`, rather than treating an
MQTT publish as a successful save. Saving from a stale revision returns an error
and preserves the current plan; repeating an already committed plan is safe.
Persistence and validation errors also preserve the last accepted plan.

Acknowledgements are published immediately, including when grid telemetry is
unavailable. All connected clients then receive the same saved plan without a
controller restart. Clearing stores `null` in the persistent file; it does not
fall back to a desktop price or a local controller file.

## Noninteractive provisioning and deployment

Use the [compact schedule example](examples/electricity-tariff.schedule.json),
replace its illustrative values, and save it as a private operator file. A
compact file has `type: "electricity-tariff-schedule"`, `version: 1`, a default
`periods` list, and optional `seasons` with names, months and their own complete
period lists. The default applies to months without an override. Periods use
`start`, `end`, `rate` and optional `days`. Season months cannot overlap.

Validate or normalize a file on the deployment machine:

```sh
python3 inverter_control/tariff.py --stdin --check < /path/to/my-schedule.json
python3 inverter_control/tariff.py --stdin --normalize \
  < /path/to/my-schedule.json > /path/to/new-electricity-tariff.json
```

The output is dashboard tariff **version 2**, with complete weekly grids. The
same command accepts existing dashboard exports, including legacy version 1
weekly plans. Input is limited to 100 KB; only normalized tariff fields are
retained. Use a new output file when redirecting `--normalize`: shell redirection
creates or truncates the destination before validation. Never redirect onto the
input file. Installation with `--install` validates first and writes atomically
with owner-only permissions to the fixed SetupHelper path; it accepts no
arbitrary destination path.

To reuse a confirmed plan on every local deployment, keep it in this checkout's
private deployment directory:

```sh
mkdir -p deploy.local
cp /path/to/my-confirmed-tariff.json deploy.local/tariff.json
./deploy.sh Cerbo
```

`./deploy.sh` (the default host is `Cerbo`) automatically selects
`deploy.local/tariff.json` when `TARIFF_FILE` is unset. The directory is ignored
by Git and excluded from deployment archives; only the validated, normalized
tariff is added to the bundle. Any provenance notes can stay alongside the file
in that directory. Keep this checkout current with the controller release before
deploying: the script installs the checkout's controller code as well as its tariff.

An explicit override takes precedence over the saved selection:

```sh
TARIFF_FILE=/path/to/my-schedule.json ./deploy.sh Cerbo
# Keep the device's Python configuration while deploying code and tariff:
PUSH_LOCAL_CONFIG=0 TARIFF_FILE=/path/to/electricity-tariff.json ./deploy.sh Cerbo
# Skip the saved deployment tariff for this run and preserve the device's plan:
TARIFF_FILE= ./deploy.sh Cerbo
```

Validation runs before SSH. The normalized plan travels with the existing
SSH deployment bundle and is validated again before stopping the controller.
It is installed at `/data/setupOptions/inverter-control/electricity-tariff.json`,
the same persistent file used by dashboard edits, not the runtime fallback file.
A selected file that is missing, unreadable or invalid stops deployment before
SSH; it never falls back to another tariff. Without an explicit `TARIFF_FILE` or
saved `deploy.local/tariff.json`, deployment leaves the device tariff unchanged.
An untracked `electricity-tariff.json` in the source directory is not implicitly deployed.
Normal release/webhook updates contain no operator tariff and preserve it.
Custom provisioning tools can install the persistent file on the device using:

```sh
python3 inverter_control/tariff.py --stdin --install < /path/to/my-schedule.json
svc -t /service/inverter-control
```

They do not need to rewrite Python configuration or provide Emporia credentials.

## Configuration precedence and invalid data

On the controller, the SetupHelper file takes priority over the local fallback
`/data/inverter-control/electricity-tariff.json`. Set `ELECTRICITY_TARIFF_FILE`
in private `local_config.py` to change that fallback. Absence means no configured
plan. A persistent JSON `null` explicitly means no tariff, even when the local
fallback exists. A malformed file at runtime logs a warning and publishes no tariff; it
never stops inverter control or silently substitutes a cheaper plan. Correct
or remove that file and restart to recover.

Controller-aware clients use the controller's plan and do not substitute a
flat default when it is absent. Older clients may still apply a saved browser
or application override; update those clients before relying on a shared
installation tariff. A legacy client override is not silently uploaded to the
controller, because it may be stale or belong to another installation.

Back up the persistent SetupHelper file to preserve the installation plan.
Normalized tariff exports remain portable and contain no account credentials,
address or consumption history. They can be imported into a compatible editor
or provisioned with the commands above; desktop configuration backups do not
replace the controller's operator-owned tariff.
