# Attributing slow acknowledged D-Bus writes

The native client records diagnostic phase durations for SetValue calls taking
at least 200 ms. It stores at most 64 records and the existing performance worker
drains them into the normal log approximately every five seconds. The write path
does not log or publish these records. A stalled sink can lose older diagnostic
records when the bounded queue fills; it cannot build an unbounded backlog.

Each record contains only a D-Bus unique sender name, message serial and durations.
Command values, message bodies, destination services and paths are omitted.

- `total_ms`: after Variant/Message construction, immediately before resolving
  the connection, through return to the synchronous caller.
- `setup_ms`: connection resolution and submission preparation.
- `dispatch_ms`: cross-thread submission until the event-loop coroutine starts.
- `await_reply_ms`: awaiting the client call, including client-side sending,
  service response, event-loop scheduling and client reply processing.
- `caller_wakeup_ms`: coroutine completion until the synchronous caller resumes.

An unavailable phase is `None`, for example when the caller times out before a
queued coroutine starts. A queued request that has expired is still never sent;
the existing cancellation, timeout and acknowledgement semantics are unchanged.
Records are copied before enqueueing, so a late cancellation cannot revise an
already reported observation.

`await_reply_ms` is **not** the wire call-to-reply measurement. Match the record's
`sender` and `serial` against a simultaneous passive `dbus-monitor --profile`
capture of the inverter SetValue path and its method return. The independent
profile supplies the service-side interval for that exact request. Because the
worker logs later, do not correlate by log emission time alone. A serial of
`None` means a wire dispatch was not observed by the client.

The native phases do not include Variant/Message construction, earlier waits
for the watchdog guard or VictronDBus `_set_lock`, CLI fallback, or the rest of
the control cycle. Compare
them with the existing `setvalue_ms` and cycle-stage measurements, but do not
label unexplained differences as proven lock contention. Record-level lock
instrumentation is a separate follow-up if the native phases do not explain a
captured tail. Percentiles from different rolling windows cannot be subtracted
to attribute a single call.

For hardware acceptance, collect a bounded passive window after identifying the
installed source. Preserve current control flags and watchdog limits. Report
deadline and write-error counter deltas alongside paired request traces. This
diagnostic change does not establish a real-time deadline guarantee.
