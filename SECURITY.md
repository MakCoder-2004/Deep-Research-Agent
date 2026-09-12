# Security Policy

## Supported versions

Only the latest code on `main` is actively supported during the MVP. Older
commits and forks may contain known vulnerabilities and are not security
releases.

## Reporting a vulnerability

Do not open a public issue, pull request, or discussion for an undisclosed
vulnerability. Use GitHub's **Report a vulnerability** action in the repository's
Security tab (Private Vulnerability Reporting), or contact the maintainer
through a private GitHub channel if that option is unavailable.

Please include:

- A clear description of the issue and its impact.
- The affected commit, version, or workflow.
- Reproduction steps or a minimal proof of concept.
- Any suggested mitigation.

Allow reasonable time for investigation and a fix before public disclosure.
The maintainer will determine the release and disclosure timeline.

## Secrets and sensitive data

Never report secrets, bot tokens, API keys, cookies, production database files,
or personal data in an issue or pull request. If sensitive data was exposed,
revoke or rotate it immediately and report the exposure privately.

The project treats retrieved web pages as untrusted input and requires SSRF
protection, redacted logs and traces, and mocked provider calls in CI. Security
changes must preserve these boundaries.
