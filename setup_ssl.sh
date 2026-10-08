#!/bin/sh
# The old dashboard and its SSL settings are no longer part of this daemon.
# Keep this entry point fail-closed for users following old instructions.
printf '%s\n' \
    'setup_ssl.sh has been retired: Inverter Control has no port-8080 web dashboard.' \
    'No certificates, trust settings, remote files, or services have been changed.' \
    'Keep the webhook, console, and metrics bound to loopback.' \
    'Use an authenticated SSH tunnel or a separately managed TLS gateway for remote access.' \
    'See docs/security-design.md for current interfaces and deployment requirements.' >&2
exit 1
