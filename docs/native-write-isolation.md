# Independent native write connection

`VictronDBus` uses one `NativeDbusClient` for reads, discovery signals and
telemetry, and another for `SetValue`. Each owns its connection and event loop.
The writer has no signal subscriptions, sender-resolution work or telemetry
reseed hook. A slow telemetry callback can therefore no longer defer writer
dispatch or reply processing on the same event loop.

The existing write lock, synchronous return value, explicit zero ACK check,
request deadline, cancellation and confirmed CLI fallback remain in place.
`NativeDbusClient`'s request implementation is unchanged. A writer transport
failure reconnects only the writer; telemetry failure does not drop that
connection. Slow-write correlation diagnostics are drained from the writer.
The write lock covers the native attempt and any CLI fallback together. A later
caller cannot overtake a failed native attempt and then be overwritten by its
older fallback. The regression exercises this scheduling interleaving with both
accepted and rejected CLI replies; it does not establish a cause of meter UDP
loss or a reduction in hardware latency.
Orderly shutdown requests polling stop, waits at most one second for the poll
thread, and closes both clients. An already running poll may finish later
(including existing CLI read fallbacks); closed native clients cannot reconnect.

The regression test blocks a real telemetry signal callback. Routing writes
through that same loop times out without dispatching a late command; routing
through the independent writer succeeds only after its explicit ACK, while
telemetry remains blocked. Separate cases cover connection failures in both
directions, write serialization, cancellation, fallback acceptance and teardown.

This isolates a client scheduling dependency, not CPU/GIL contention or the
physical bus service. Matched live observations on the preceding build showed
that native-await duration substantially exceeded wire call-to-reply duration;
they do not by themselves prove that telemetry explained the whole difference.
The candidate still needs a separate hardware timing window with matched sender
and serial before claiming an improvement.
