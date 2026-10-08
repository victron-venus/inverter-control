These files contain public Diffie-Hellman group parameters, not private keys.

- `dh2047.txt`: generated with OpenSSL 3.6.4 `openssl dhparam 2047`.
- `dh2048.txt`: standard FFDHE2048, from `openssl genpkey -genparam -algorithm DH -pkeyopt group:ffdhe2048`.

The loopback test parses the exact prime size and calibrates both groups with a
verified, test-only lower-security client before exercising the real clients.
