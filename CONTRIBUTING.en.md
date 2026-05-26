# Contributing

> 🌐 [中文版](./CONTRIBUTING.md)

Issues and pull requests are welcome. This guide describes the common
flow; for non-trivial direction changes, open an issue to discuss
before writing code.

## Development setup

```bash
git clone git@github.com:TMYTiMidlY/portal-mcp-server.git
cd portal-mcp-server
uv sync --all-extras           # creates .venv with prod + dev deps
source .venv/bin/activate
pytest                         # should be all green (live SSH tests skip by default)
```

To point an MCP client at this local checkout, install it as a fixed executable:

```bash
uv tool install --force .      # --force overwrites the old tool with this checkout
```

If you'd rather not use uv, plain pip works:

```bash
pip install -e ".[dev]"       # -e/--editable points at this source tree
```

## Code conventions

- **Python 3.10+**; type-annotate new code, especially public functions
  and MCP tool signatures.
- All I/O is `async/await`. Blocking calls inside the server process
  (`time.sleep`, `subprocess.run`, sync `socket.recv`, …) are **not
  acceptable**.
- No hardcoded hostnames, usernames, IPs, or paths — read from
  `config/hosts.yaml` or environment variables.
- Use `shlex.quote` / `quote_shell` when building shell commands.
  **Never** f-string user input directly into a command — see the
  validators in `safety.py` for the existing patterns.
- Changes to security-critical code (`security.py`, `cli.py:_gate*`)
  must come with tests covering both the happy path and rejection
  paths.

## Adding a new tool

When adding a new `@mcp.tool()`:

1. **Reconsider whether you need a new tool.** Prefer extending an
   existing `portal_*` tool's `mode` / `action` parameter. The
   "18 vs 57" tool-budget framing in the README is a deliberate
   design constraint — don't blow it without a reason.
2. **Write a complete docstring.** FastMCP exposes the docstring
   verbatim as the MCP tool description; the quality of the docstring
   directly determines whether the agent uses the tool correctly.
3. **Wire the security gate.** Any state-changing operation must call
   `_gate(host, command)`; multi-host operations use `_gate_many` /
   `_gate_playbook`.
4. **Emit an audit entry.** State-changing happy paths end with
   `audit_log(host, op_str, result, operation="...")`. Read-only
   tools intentionally skip auditing.
5. **Update [`docs/tools.md`](./docs/tools.md).** It's the
   user-facing tool index; keep it in sync when you add or change a
   tool's modes.
6. **Tests** — at minimum one happy path and one policy-reject path.
   Use the recorder pattern in
   `tests/test_pool_leak_regression.py` for resource-lifecycle tests.

## Testing requirements

- `pytest tests/ -v` must be **all green** before you submit (live
  SSH tests are skipped by default and don't need real hosts).
- For bug fixes: **write the regression test first**, then fix the
  code. This prevents the bug from coming back.
- Security and resource-lifecycle fixes get tests in
  `tests/test_pool_leak_regression.py` or
  `tests/test_gate_coverage_fixes.py`.
- For end-to-end verification, use `tests/live_smoke.py` — it needs a
  real SSH host (see the README "Testing" section).

## Security & privacy

- **Never commit secrets.** `hosts.yaml` and any file with real
  hostnames / usernames / private-key paths are in `.gitignore`. Don't
  bypass it.
- `git diff` your branch before pushing to confirm no local paths,
  IPs, or tokens leaked into a docstring or test fixture.
- `config/hosts.example.yaml` is the **only** schema template; update
  it whenever you add a config field.

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/).
Use the right type prefix:

| Prefix       | When to use                                             |
|--------------|---------------------------------------------------------|
| `feat:`      | A new feature (tool, mode, CLI flag, …)                 |
| `fix:`       | A bug fix                                               |
| `security:`  | Security vulnerability fix or hardening                 |
| `docs:`      | Documentation-only changes                              |
| `test:`      | Test-only changes                                       |
| `refactor:`  | Behaviour-preserving refactor                           |
| `chore:`     | Build scripts, CI, dependencies, examples, etc.         |

Add a scope where it helps: `fix(remote-edit): …`,
`security(audit): …`.

If a commit does several things, **split it** — one logical change per
commit makes review and revert dramatically easier.

## Pull requests

1. Fork → develop on a feature branch.
2. Use a Conventional Commits-style PR title.
3. The PR description should include:
   - **What** the PR does
   - **Why** (link to issue / user scenario / upstream advisory)
   - **How** — key design decisions, if any
   - **Test plan** — what you ran and what passed
4. CI must be green: ruff lint + pytest.
5. PRs that touch behaviour or security generally need a maintainer
   review; pure docs / test changes can land faster.

## Vulnerability disclosure

**Do not** report security vulnerabilities in public issues. Use
GitHub Security Advisories per [`SECURITY.md`](./SECURITY.md).

## License

By submitting a pull request you agree that your contribution is
released under the Apache License 2.0 (see [`LICENSE`](./LICENSE)).
