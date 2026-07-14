# 0004 — Exec defaults to a login shell; `use_sudo` fully becomes root, with no "keep-my-env sudo" mode

Status: accepted

> 🌐 [中文版](./0004-login-shell-and-sudo-env.md) ｜ English

`remote_exec` / `remote_job` default to `login=True`: the command runs in a
**login shell** (`bash -lc`) that inherits the login environment of *whoever the
command runs as* (`/etc/profile` + the `~/.bash_profile`/`~/.profile` chain), so
conda / nvm / pyenv / `~/.local/bin` PATH additions apply. `use_sudo=True` means
**fully becoming root**: sudo's default `env_reset` re-initializes
`HOME`/`USER`/`LOGNAME`/`SHELL` from the target user (root), so `~` expands to
`/root` and the login user's environment is not preserved — user-owned files
must be addressed by absolute path. portal deliberately does **not** offer a
"sudo privileges but keep my user environment" mode.

## Context

Two independent things that are easily conflated:

1. **Login-shell inheritance.** A non-login, non-interactive `bash -c` reads no
   rc at all; PATH is the bare default and rc/profile-installed tooling
   (conda/nvm/pyenv) is gone. `login=True` (default) uses `bash -lc` to load the
   login environment.
   - Honest boundary: `bash -lc` is **login + non-interactive**. It reads
     `/etc/profile` and the `~/.bash_profile`/`~/.profile` chain. It *can* load
     `~/.bashrc`; the real blocker is the **non-interactive guard**: the default
     Ubuntu/Debian `~/.bashrc` starts with `case $- in *i*) ;; *) return;; esac`,
     which `return`s under a non-interactive shell (`$-` without `i`), skipping
     the conda/nvm/pyenv init placed **below** it — tested: a guarded `bash -lc`
     misses it; drop the guard, or put the export above it / in `~/.profile`, and
     it loads. The only thing that makes the guard pass is an **interactive
     shell** (`bash -ic`), but in a tty-less SSH exec that floods stdout with
     `/etc/bash.bashrc` output, stderr with `no job control` warnings, and (with
     `-l`) the full MOTD, and needs a PTY (which collides with the sudo/secret
     stdin channel). **More fundamentally, portal respects the target's guard** —
     gating that content to interactive sessions is a deliberate choice by the
     user/distro (there for a reason), not something automation should force open
     with `bash -ic`. So portal uses non-interactive `bash -lc`: to make a tool
     available here, put its PATH/init in `~/.profile` or above the guard, not in
     the interactive `.bashrc` body.

2. **`use_sudo` identity semantics.** `sudo <cmd>` (no `-u`) necessarily runs as
   root. sudo's default `env_reset` (this host's `sudoers(5)`: *"The HOME, MAIL,
   SHELL, LOGNAME and USER environment variables are initialized based on the
   target user"*) rebuilds the environment for root. So **even** a `bash -lc` on
   top loads **root's** login environment with `~` = `/root` — never the login
   user's.

## Considered options

- **A separate "keep user env + sudo" mode (`sudo -E` / `--preserve-env`)** —
  rejected.
  - `-E` ("disable `env_reset` from the command line") needs an **extra grant on
    top of ordinary sudo rights** — the sudoers `setenv` option, or a `SETENV`
    tag on the command rule (`sudoers(5)`: implied only when the rule's command
    is `ALL`). This is distinct from "does the user have sudo at all": on a host
    that narrows sudo to a specific command whitelist (least privilege), plain
    `sudo <cmd>` runs fine, yet `sudo -E <cmd>` is refused with
    `sorry, you are not allowed to preserve the environment` (unless that rule is
    explicitly tagged `SETENV`). So whether `-E` works **depends on the target's
    sudoers grant shape** (a broad `ALL` implies it; a command whitelist does
    not), which portal can't know — defaulting it on means "works on
    broadly-granted hosts, silently refused on least-privilege ones":
    non-deterministic, unfit for a default.
  - Security: with `env_reset` disabled, `-E` inherits into root **everything
    except the `env_check`/`env_delete` blacklist** (the classic dynamic-linker
    vectors — `LD_*`, `IFS`, `BASH_ENV`, `ENV` — are indeed still stripped), so
    `PYTHONPATH` / `PERL5LIB` / `NODE_OPTIONS` / arbitrary app-specific vars
    still ride along. `sudoers(5)` itself notes it is "not possible to block all
    potentially dangerous environment variables" and recommends the default
    `env_reset`; and here the environment is agent/LLM-influenced on top.
    `--preserve-env=HOME` narrows it but still needs that `SETENV` grant and
    still hands a user-writable rc to root.
  - Semantics: `use_sudo=True` yet "not root" is a self-contradictory
    pseudo-state; a `root=true/false` knob would only muddy the signature.
- **`export HOME=<login user's home>` inside the elevated shell, then `bash -lc`
  (no sudoers change)** — rejected (explicitly no longer pursued). It sidesteps
  the `setenv` dependency but: (1) still has root source the login user's rc
  (same privesc smell); (2) is bounded by the same `~/.bashrc` non-interactive
  guard, so "get conda/nvm back" — the main selling point — doesn't actually
  materialize; (3) needs to resolve the login user's home and splice the
  injection, buying complexity for a thin payoff.
- **`use_sudo` is a full switch to root, with login-shell inheritance on by
  default** — chosen. Clean semantics (elevation = becoming root, same as system
  `sudo`), zero sudoers dependency, works on any host; the "I want my
  environment" need is met by absolute paths plus putting the tools you need in a
  login-safe location (profile / system-wide).

## Consequences

- `remote_exec` / `remote_job` default to `login=True` → `bash -lc`;
  `login=False` falls back to a bare `bash -c`. Operators tune the default via
  `PORTAL_LOGIN_SHELL` or a per-host `login_shell:`.
- Under `use_sudo=True`, `~`/`$HOME` = `/root` and `$USER`/`$LOGNAME` = root:
  **always use absolute paths for login-user files.** This is by design, not a
  bug.
- Docs (tool docstrings / README) must not promise that login loads a guarded
  `~/.bashrc`; the wording is "loads the login environment (the profile chain);
  the guarded `.bashrc` body is excluded non-interactively."
- Making a tool available under elevation / non-interactively is done by putting
  its PATH/init in `~/.profile` or a system-wide location, not by expecting
  portal to replicate an interactive shell.
- The vocabulary — "Login shell", "Login environment", "sudo = become root" — is
  defined in [`CONTEXT.md`](../../CONTEXT.md).
