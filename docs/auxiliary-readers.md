# Auxiliary readers and control timing candidate

Water and EV D-Bus reads run in two dedicated background workers. The control
loop and console only copy cached values; a blocked device read cannot execute
synchronously through `read()`. Each worker waits two seconds after a refresh.
A snapshot older than four seconds, an initial read, or a failed refresh yields
unknown fields. Age starts before the I/O pass, so a slow result is not relabelled
fresh when it finally arrives. Stopping clears cached data and joins for at most
one second per worker; it never falls back to synchronous I/O.

The existing control policy maps unknown EV power to zero. This candidate does
not change or qualify that policy. It needs device acceptance with the installed
EV-dependent flags before deployment, including startup, stale data, EV service
loss/recovery and charger transitions. Water/EV reads are confirmed synchronous
call sites in the previous code; that does not prove they explain all observed
cycle overruns. Background work may still contend for CPU or the D-Bus service.

Cycle telemetry now publishes `window_capacity` and per-series `samples`.
Percentiles retain the existing rounded zero-based index algorithm. Do not merge
overlapping snapshots or interpret cache age as physical command latency.

## Candidate boundaries

The acceptance workspace contains two separate changesets: one based on current
main, and a narrow backport based on installed `1.23.4-beta.22` / `26520fd`.
Only the latter can be compared directly with the recorded Cerbo baseline.
Neither was installed, and there are no post-fix hardware latency numbers.

Before an operator rollout, verify the installed source hashes and capture modes,
control flags, limits and process identity again. Existing `_load_control_flags`
resets all flags to false on cold start, including the observed OnlyCharging and
DoNotSupplyCharger flags. A restart is therefore a change to physical control
behavior, even if configuration files are retained. Establish an approved safe
plant state and a flag restoration/verification procedure before restarting;
ordinary file rollback also restarts the process and has the same caveat.

After controlled installation, repeat the passive capture with exact candidate
hashes, sample counts, deadline counters and write/read-back evidence; then run
the separately approved fault/physical-settling cases. Local blocked-reader tests
qualify only the scheduling boundary, not the installation or physical response.
