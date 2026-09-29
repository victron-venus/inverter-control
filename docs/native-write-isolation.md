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
Transport-failure diagnostics run only after both attempts and after releasing
the lock, so a blocked logger cannot delay fallback or a subsequent zero write.
Orderly shutdown signals all managed workers before waiting on any of them.
Their joins share one five-second budget, including MQTT callback drain and the
hardware watchdog. MQTT's public disconnect/loop-stop calls run on an owned
daemon cleanup thread. Shared native close is requested only after the MQTT
callback, watchdog, polling and auxiliary readers are confirmed stopped. If a
worker is still active, shutdown reports incomplete cleanup and leaves shared
I/O open rather than interrupting an in-flight hardware write.

A successful cleanup result means managed workers stopped and native close was
requested. Native event-loop stop is asynchronous; its thread exit is not
acknowledged by this result. Logging, kernel calls and whole Python process
termination are not hard bounded. The external installer's two-second graceful
stop interval is unchanged, so this source change does not promise retirement
before that deadline or establish a hardware timing improvement.

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
