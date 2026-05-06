# Security Policy

## Reporting a vulnerability

If you believe you've found a security vulnerability in Seshat, please
**do not** open a public GitHub issue.

Instead, report it privately via GitHub Security Advisories:

1. Go to the [Security tab](https://github.com/malevolenttortoise/seshat/security/advisories/new)
2. Click **Report a vulnerability**
3. Fill out the form with as much detail as you can provide

You can expect an initial response within a few days. If the report is
confirmed, a fix will be prioritized for the next release and credited
in the changelog (unless you'd prefer to remain anonymous).

## Scope

Seshat is a self-hosted application. The security model assumes:

- The web UI is reachable only by the operator (typically on a LAN or
  behind a reverse proxy / VPN), not the open internet.
- The single admin account is trusted; there is no multi-user
  authorization model to bypass.
- Credentials (tracker session, IRC password, torrent client password,
  ntfy token, provider API keys) are stored in an encrypted store on
  disk; the Fernet key lives beside the database in `auth_secret`.

**In scope:**

- Authentication bypass (accessing the UI or API without a valid session)
- Credential exposure (leaking stored secrets via logs, error pages,
  API responses, or debug endpoints)
- Path traversal or arbitrary file read/write in the sync/ingest paths
- SSRF in any scraper or webhook handler
- SQL injection in any database query
- Dependency vulnerabilities with a practical exploit path in Seshat

**Out of scope:**

- Anything requiring physical access to the host
- Anything requiring a pre-existing admin session
- Vulnerabilities in upstream dependencies with no Seshat-reachable
  code path (report those upstream)
- Denial of service via unrealistic resource consumption (e.g.
  pointing Seshat at a 10M-book Calibre library)

## Supported versions

Only the latest `main` / `ghcr.io/malevolenttortoise/seshat:latest` image is
supported. There is no backport policy for older tagged releases.
