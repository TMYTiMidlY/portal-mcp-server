# Contributing

The contribution guide is consolidated into the project README:

> 👉 **[README · 贡献](./README.md#%E8%B4%A1%E7%8C%AE)** (Chinese)
> 👉 **[README · Contributing](./README.en.md#contributing)** (English)

Highlights (the full list lives in the README):

- Python 3.10+, type-annotated where reasonable; everything I/O is
  `async/await` — no blocking calls.
- No hardcoded hostnames, usernames, IPs, or paths — always read from
  config.
- Every new tool needs a clear docstring (FastMCP uses it as the MCP
  tool description) and an entry in
  [`docs/tools.md`](./docs/tools.md).
- Cover the critical paths with tests; `pytest tests/ -v` must pass.
- Don't commit secrets, real hostnames, or personal credentials;
  `config/hosts.example.yaml` is the only schema template.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:` / `fix:` / `docs:` / `chore:` / …).

This file exists so that GitHub auto-discovers the contribution guide
and surfaces a *Contribute* nudge in the issue / PR creation flow; the
canonical content lives in the README.
