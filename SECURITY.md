# Security Policy

## Supported versions

Security fixes land on the latest minor of each published package:

| Package         | Supported      |
| --------------- | -------------- |
| `runspec` (PyPI)| Latest 0.x.y   |
| `runspec-node` (npm) | Latest 0.x.y |

Older versions do not receive backports — upgrade to the latest minor to pick up fixes.

## Reporting a vulnerability

**Please do not open public issues for security problems.**

Use GitHub's private vulnerability reporting:

1. Go to the [Security tab](https://github.com/jasonfinestone/runspec/security) of this repo
2. Click **Report a vulnerability**
3. Describe the issue, ideally with a reproduction and the affected version

You should get an acknowledgement within 7 days. Once a fix is ready, a coordinated release goes out to PyPI / npm, and the advisory is published with credit (unless you prefer to remain anonymous).

## Scope

In scope:
- Argument parsing, type coercion, and validation in `runspec` and `runspec-node`
- The `runspec serve` MCP stdio server
- Logging behaviour and any code that touches the filesystem or subprocess

Out of scope:
- Vulnerabilities in user-installed runnables (their `runspec.toml` configs are out of our control)
- Vulnerabilities in upstream dependencies (report those upstream; we'll pull in fixes via Dependabot)
