# Electricity tariffs during installation and deployment

Electricity tariffs describe dashboard energy prices and billing periods. They
are optional and independent of the controller's expensive-hours settings,
battery charging policy and inverter setpoints. No price is assumed when a
plan is missing. Emporia is one possible source; manual tariffs work without
an Emporia account or credentials.

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

To configure an already installed system directly:

```sh
cd /data/inverter-control
python3 inverter_control/tariff.py --interactive --install
svc -t /service/inverter-control
```

Restart the controller after changing the file; clients receive the new plan
through its existing `ui_config.electricity_tariff` state field. The tariff is
read at startup, not watched continuously.

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

Deploy a plan explicitly:

```sh
TARIFF_FILE=/path/to/my-schedule.json ./deploy.sh Cerbo
# Keep the device's Python configuration while provisioning only the tariff:
PUSH_LOCAL_CONFIG=0 TARIFF_FILE=/path/to/electricity-tariff.json ./deploy.sh Cerbo
```

Validation runs before SSH. The normalized plan travels with the existing
SSH deployment bundle and is validated again before stopping the controller.
It is installed at the persistent SetupHelper path. Without `TARIFF_FILE`,
deployment leaves the device tariff unchanged. An untracked
`electricity-tariff.json` in the source directory is not implicitly deployed.
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
plan. A malformed file at runtime logs a warning and publishes no tariff; it
never stops inverter control or silently substitutes a cheaper plan. Correct
or remove that file and restart to recover.

Updated dashboards apply this order:

1. A tariff explicitly saved in that dashboard's local editor.
2. A desktop/mobile application configuration tariff, if present.
3. The tariff advertised by inverter-control.

Use **Use installation tariff** in the dashboard editor to remove the local
override. Installation changes then appear automatically after the controller
restarts. Local overrides remain local; editing them does not write back to
SetupHelper, another installation, or Emporia. Update older dashboard versions
before provisioning a seasonal plan; they do not consume this installation field.

## Desktop/mobile setup, settings and backups

The first-run wizard and **Configuration → Electricity tariff** provide the
same editor. Enter prices manually, use **Add season**, select calendar months,
and set the billing start day; or import normalized dashboard JSON. **Apply
tariff** changes the configuration draft. **Save & Continue** or Configuration
**Save** persists it. Closing without saving discards those draft changes.
**Use controller tariff** clears the application-level plan; any local dashboard
override still takes priority until removed there too.

The application stores the plan in its existing encrypted configuration and
includes it in portable configuration backups:

```json
{
  "modules": {
    "victron.energy-tariff": {
      "schema_version": 1,
      "values": { "plan": null }
    }
  }
}
```

For managed installations, replace `null` with the complete normalized tariff
object and merge this namespace into the installation's regular configuration
backup before restoring it. Do not replace the whole configuration with this
fragment. `null` means inherit the controller tariff. The module envelope version
is 1; the contained tariff version is 2. Unsupported or invalid plans remain
visible as configuration errors and are not used for pricing. Other modules and
connection credentials retain their existing backup/restore behavior. Tariff
exports contain no account credentials, address or consumption history.

For web-only installations, provision the controller file or import the
normalized JSON through **Set tariff**. Browser-only overrides are not part of
desktop configuration backups; use **Export tariff** to transfer those copies.
