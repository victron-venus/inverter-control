# Credentials and rotation

Home Assistant credentials can be stored separately from Python configuration.
Use `HA_TOKEN_FILE` for new installations. Existing `HA_TOKEN` configurations
continue to work; if a nonempty `HA_TOKEN_FILE` is set, its contents take
precedence. The example leaves Home Assistant disabled until a token is supplied.

## Provision a Home Assistant token

Create a dedicated Home Assistant account with only the access needed for the
configured entities. Provision its long-lived access token through Home
Assistant's account settings. On Venus OS, use the deployment administrator to
create a private directory outside the replaceable application directory, for
example `/data/setupOptions/inverter-control/credentials`, with mode `0700`.
Write the token to `ha.token` inside it using a private editor or a secure file
transfer. Do not paste it into a command line, shell history, a ticket, or logs.

The token file must:

- Be a regular file, owned by the account running the daemon (normally root on
  Venus OS), with mode `0600` or `0400`.
- Contain just the token, optionally followed by a single LF or CRLF line ending,
  and occupy at most 16384 bytes, including that line ending.
- Use an absolute path. Symlinks, devices, FIFOs, directories, group/world access,
  and executable or special permission bits are rejected.

The bearer-token character set is ASCII letters, digits, `.`, `_`, `~`, `+`, `/`,
`-`, and optional trailing `=` padding. Home Assistant's JWT token fits this
format. The loader does not print the token or include it in an error message.
Protect every parent directory against modification by other accounts: the
loader rejects a symlink at the final path, but does not secure an
administrator-writable directory tree against its administrator.

Set the following in `local_config.py` after the file is ready:

```python
HA_URL = "https://ha.example.org:8123"
HA_TOKEN_FILE = "/data/setupOptions/inverter-control/credentials/ha.token"
```

`HA_TOKEN` may be omitted or empty in file mode. An unreadable, missing,
misowned, over-permissive, or malformed selected file stops configuration
loading. It never silently falls back to an old inline token. Test the file and
configuration before a planned service restart, with a site-specific safe
operating state and access to the previous working configuration.

## Rotate without rebuilding

The daemon reads the file once during startup; it does not watch the file or
reload credentials while a request is in flight.

1. Create a replacement token at the provider. If rotating a known exposed
   token, revoke the exposed token immediately and accept the integration
   interruption while replacing it.
2. Prepare a new regular file in the same private directory using the same
   owner and mode. Keep the current path intact until the replacement is ready.
3. Atomically rename the new file over `ha.token` on the same filesystem. This
   prevents a new process from seeing a partially written token.
4. Restart the supervised daemon during the planned maintenance window and
   check Home Assistant integration health. A restart also resets in-memory
   control state; follow the [operations guide](venus-os-operations.md).
5. For routine rotation, revoke the old token after the new connection works.
   Remove old inline copies and temporary files from private configuration and
   backups according to the site's backup policy. An old backup must not retain
   an active credential indefinitely.

Neither source changes nor recompilation are needed. A failed replacement must
be fixed explicitly; restarting will not revive the old inline credential.
The updater does not create, copy, chmod, or delete this externally provisioned
file. Keep it outside `/data/inverter-control` so application replacement cannot
remove it. Uninstalling the application does not revoke the provider's token.

## Loki authentication with the Requests transport

For authenticated Loki Basic authentication, use the supported Requests
transport and its separate netrc credential file. Put the service account's
username and password/token in a private file and set `NETRC` to its
absolute path in the **log-forwarder service's** environment. Requests also
supports the service account's default `~/.netrc`, but an explicit `NETRC` path
lets the fallback detect that authentication was requested.
Do not put credentials in `LOKI_URL` or the ordinary endpoint file. The netrc
file is a credential store; its `machine` field identifies the receiving host.

The shape is `machine HOST login USER password SECRET`, replacing the three
placeholders privately. Keep that file and its parents owner-controlled, with
file mode `0600`. Include only the intended Loki hostname; do not use a
`default` entry, and do not share credentials across Home Assistant and Loki
hosts. Requests consults netrc for other connections too, so a broad entry can
override an unrelated Authorization header. Use HTTPS for a remote receiver.

The forwarder creates a fresh Requests session for each batch. Atomically
replacing the netrc file updates credentials for the next batch without code
changes, recompilation or a controller restart. Revoke the old credential at
the provider after routine rotation, or immediately on exposure. The actual
Requests path is covered by a loopback test that observes the two different
Authorization headers before and after replacement. A separate two-origin
redirect test confirms that neither credentials nor the log body is forwarded
to the redirect destination.

The optional standard-library fallback does not implement netrc authentication.
It rejects URL userinfo and a nonempty explicit `NETRC` setting before network
access, retaining the cursor for retry. It does not search a default `~/.netrc`;
use explicit `NETRC` when authentication is required. Install the declared
Requests dependency for authenticated Loki, or terminate
authentication in a separately managed local encrypted tunnel/gateway. A missing
or mismatched netrc entry can cause an unauthenticated request and a 401/403;
it does not make an incorrectly exposed public Loki endpoint private. The
forwarder retains its cursor on a rejected push. Operator URL credentials
remain a legacy Requests feature but are not the recommended credential store.

## Other credentials and trust material

SSH deployment uses the operator's separate SSH private keys, agent, known-hosts
file, and host configuration. Replace or revoke keys using OpenSSH and the
server account's authorized keys; preserve host-key verification. The daemon
has no SSH private key compiled into it. Operator-configured jump hosts are a
separate trust boundary.

HTTPS trust roots are provided by the platform or configured CA files. The
daemon exposes no dedicated mutual-TLS client-key configuration setting.
External gateways own their separate certificate/private-key files; the
application does not provision or rotate them. Follow the owning platform's
procedure and restart affected gateway sessions/services when required. Do not
weaken the certificate/key policy to make rotation pass.

The MQTT client and local webhook/console/metrics listeners do not accept
application authentication credentials; protect them at the broker, gateway,
and host boundaries described in the [security design](security-design.md).
If adding new credentials or private keys, keep their secret bytes in separate
private files and document replacement, provider revocation, and process reload.
