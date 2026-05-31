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
5. **Update the README Tools section** in [`README.md`](./README.md) /
   [`README.en.md`](./README.en.md). The "Tools" section (including the
   collapsible full-signature + source-map tables) is the user-facing
   tool index; keep it in sync when you add or change a tool's modes.
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

> 📌 **This project uses [Commitizen](https://commitizen-tools.github.io/commitizen/)
> to manage the version number and CHANGELOG** (`[tool.commitizen]` in
> `pyproject.toml`). `cz bump` reads these conventional-commit prefixes to
> decide the semver increment: `feat` → minor, `fix` / `security` → patch,
> a `BREAKING CHANGE` footer or `!` → major. A wrong prefix makes the
> automatic version bump wrong — get it right. `docs` / `test` /
> `refactor` / `chore` don't trigger a bump.

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

## CI & Release automation

The repo runs two GitHub Actions pipelines. Normal releases never
require a local `python -m build`.

### CI — `.github/workflows/ci.yml`

Triggers: `push` / `pull_request` to `main`.
Matrix: Python **3.10 / 3.11 / 3.12 / 3.13** (ubuntu-latest).

Each matrix job does four things:

1. `pip install -e ".[dev]" && pip install ruff`
2. `ruff check portal_mcp_server/` — lints product code only, not tests
3. Import smoke: `python -c "import portal_mcp_server; assert portal_mcp_server.main"` plus `portal-mcp-server --help`
4. `pytest tests/ -v --tb=short` (live SSH fixtures skip by default, so
   CI never needs a real host)

A PR can only land when all four Python versions are green.

### Release — `.github/workflows/release.yml`

Triggers:
- pushing a tag matching `v*.*.*` (**production path**)
- `workflow_dispatch` (manual fallback)

Three jobs run in order:

1. **`release-build`** — `python -m build` produces wheel + sdist,
   uploaded as artifact `release-dists`.
2. **`create-release`** — downloads the artifact, awk-extracts the
   matching version's section from `CHANGELOG.md` (see "CHANGELOG
   format constraint" below), and uses `softprops/action-gh-release@v1`
   to create a GitHub Release with the `.whl` / `.tar.gz` attached.
3. **`pypi-publish`** — uses the official PyPA action with **OIDC
   trusted publishing** (no token, no secret) to publish to PyPI.
   `skip-existing: true` prevents a hard failure on republish of an
   existing version.

The GitHub environment `pypi` is bound in repo Settings → Environments
to the trusted publisher at https://pypi.org/p/portal-mcp-server/.
**Do not stash a `PYPI_API_TOKEN` there** — trusted publishing is
strictly more secure than a static token.

### CHANGELOG format constraint

`release.yml` uses awk to grab the section that starts with `## ` and
whose first line contains the target version string, up to (but not
including) the next `## `:

```markdown
## v1.1.0 (2026-05-26)

### BREAKING CHANGES
- ...

### Fix
- ...

## v1.0.1 (2026-05-15)
...
```

> ⚠️ Each version header must contain the plain version number (e.g.
> `1.1.0`), or awk returns an empty string and the GitHub Release body
> ends up blank. The `## v<x.y.z> (<date>)` header that `cz bump`
> generates satisfies this constraint out of the box.

### Cutting a new release

> ## ⚠️ Release rules (TL;DR)
> 1. **Do not hand-edit** `version` in `pyproject.toml`
> 2. **Do not hand-write** `CHANGELOG.md`
> 3. **Do not run** `uv lock` manually
>
> Releasing is **a single command**: `uv run cz bump` — with `version_provider = "uv"`, `cz bump` updates `pyproject.toml`, the self-version inside `uv.lock`, and `CHANGELOG.md`, then folds all of it into the same bump commit + the same annotated tag.

**The version number, CHANGELOG, and `uv.lock` are all managed by
[Commitizen](https://commitizen-tools.github.io/commitizen/)** —
`pyproject.toml` sets `version_provider = "uv"`, so `cz bump` also
updates the self-version inside `uv.lock` and includes it in the same
bump commit. **Do not hand-edit `version` in `pyproject.toml`, do not
hand-write `CHANGELOG.md`, and you don't need to run `uv lock`
manually.**

1. Make sure everything to ship is merged into `main` and you're on a
   clean `main` HEAD.
2. Local pre-flight: `pytest tests/ -v && ruff check portal_mcp_server/`.
3. Preview the version and CHANGELOG: `uv run cz bump --dry-run`.
4. Release: `uv run cz bump` — a single command that bumps `version`
   in `pyproject.toml` per the commit history, updates `uv.lock`,
   prepends a `## v<x.y.z> (<date>)` block to `CHANGELOG.md`, creates
   the bump commit, and tags an annotated `v<x.y.z>` (`annotated_tag = true`).
5. Push to trigger the release: `git push origin main --follow-tags`.
6. Watch the [Actions page](https://github.com/TMYTiMidlY/portal-mcp-server/actions)
   until all three jobs go green.
7. Verify: https://github.com/TMYTiMidlY/portal-mcp-server/releases/tag/v\<x.y.z\>
   and https://pypi.org/project/portal-mcp-server/\<x.y.z\>/.

> `cz bump` does not push on its own (so you can still back out).
> `--follow-tags` pushes the annotated tag together with `main`;
> release.yml is triggered by `push: tags: ['v*.*.*']`.

### When a release fails

| Red job | Most likely cause | What to do |
|---|---|---|
| `release-build` | broken `pyproject.toml` / build backend error | Read the build log; reproduce locally with `python -m build` |
| `create-release` | CHANGELOG section not extractable (version string missing from header / earlier header truncated) | Usually a hand-edit broke the format — prefer what `cz bump` generates; delete the tag, fix, retag |
| `pypi-publish` | trusted publisher not configured / version already on PyPI | Configure trusted publishing; `skip-existing` already accepts a duplicate version so no re-upload needed |

> History lesson: before v1.1.0, `release.yml` read the version from
> `pyproject.toml` and risked drifting from the tag name. Commit
> `8e33ea3 fix(ci): derive release version from tag name instead of
> pyproject.toml` switched to `GITHUB_REF_NAME`, which is the source
> of truth.

## Vulnerability disclosure

**Do not** report security vulnerabilities in public issues. Use
GitHub Security Advisories per [`SECURITY.md`](./SECURITY.md).

## License

By submitting a pull request you agree that your contribution is
released under the Apache License 2.0 (see [`LICENSE`](./LICENSE)).
