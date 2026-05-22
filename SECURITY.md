# Security Policy

PKM Brain is local-first software that may process private notes, meeting transcripts, agent logs, and personal knowledge artifacts. Please treat reports involving data exposure, unsafe sync behavior, authentication bypasses, or secret handling as security-sensitive.

## Supported Versions

The `main` branch is the active development line. Security fixes target `main` unless release branches are introduced later.

## Reporting a Vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting when available for this repository. If that is not available, contact the repository owner directly and avoid posting exploit details in a public issue.

Useful reports include:

- affected command, MCP tool, or workflow
- minimal reproduction steps
- expected versus actual behavior
- whether private runtime data, credentials, or local files may be exposed

## Data Handling Expectations

- Runtime knowledge workspaces should remain outside git, normally under `~/brain`.
- Secrets should be stored in local environment/configuration, not committed files.
- MCP access should be granted only to trusted local agents.
- LAN-visible services or sync paths should be enabled only with explicit authentication and network intent.
