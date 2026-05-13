# Security Policy

The full, up-to-date security posture lives in the project README so it
sits next to the rest of the documentation:

> 👉 **[README · Security](./README.md#%E5%AE%89%E5%85%A8)** (Chinese)
> 👉 **[README · Security](./README.en.md#security)** (English)

That section covers:

- Default constraints and the `/tmp/`-by-default sandbox convention
- The `SecurityPolicy` gate (host allowlist, command blocklist /
  allowlist, per-host rate limit) and which entry points are gated
- Key-only authentication; rejection of `password:` fields
- The audit log (`logs/audit.jsonl`), fail-closed default, and the
  honest disclosure about the `audit-after-operation` window
- The hash-protected remote-edit algorithm and its provenance
- Operator-side hygiene recommendations
- Supported branches

## Reporting a vulnerability

**Please don't open a public GitHub issue for security vulnerabilities.**
Use [GitHub Security Advisories](https://github.com/TMYTiMidlY/portal-mcp-server/security/advisories/new)
to report privately. Targets:

- Acknowledgement within 48 hours
- Initial assessment within 7 days
- Critical fixes shipped within 30 days

This file exists so that GitHub auto-discovers the security policy and
shows the "Report a vulnerability" link from the repository's *Security*
tab; the canonical content lives in the README.
