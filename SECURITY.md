# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.3.x   | :white_check_mark: |
| < 1.3   | :x:                |

## Reporting a Vulnerability

Private vulnerability reporting is enabled for this repository. Use
[Report a vulnerability](https://github.com/victron-venus/inverter-control/security/advisories/new)
to send a confidential report to the maintainers. Follow
[GitHub's private reporting instructions](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
if you need help submitting the report.

Include the affected version or commit, steps to reproduce, expected and actual
behavior, and potential impact. Remove access tokens, credentials and personal
data from examples. Do not disclose exploit details in public issues before
coordinating with the maintainers.

## Security Considerations

This project runs on Venus OS with access to:

- D-Bus (Victron system control)
- Home Assistant API (if configured)
- Local network (web interface)

### Recommendations

1. **secrets.py**: Never commit this file. It contains API tokens and sensitive configuration.
2. **Network**: Run on a trusted local network. The web interface has no authentication.
3. **SSL**: Use HTTPS in production (see `setup_ssl.sh`).
4. **Firewall**: Consider restricting access to ports 8080 (web) and 9999 (console).

## Known Limitations

- Web interface has no authentication
- TCP console stream has no encryption
- Designed for trusted home networks only
