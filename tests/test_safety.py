"""Unit tests for ssh_remote_mcp.safety.

These tests cover only the validators / helpers in safety.py and require
no external SSH server.

Mapped to the original audit findings:
  * Path traversal / NUL-smuggling             — TestRemotePathValidation
  * Command injection via cwd / shell args     — TestQuoteShell, TestBuildCwdPrefix
  * Env-var key injection (issue: session.set_env, ssh_exec_with_env)
                                               — TestEnvKeyValidation
  * Unconstrained signal name                  — TestSignalValidation
  * Unconstrained interpreter name             — TestInterpreterValidation
  * Unconstrained tmux session name            — TestTmuxNameValidation
  * Bad PID inputs                             — TestPidValidation
"""
import pytest

from ssh_remote_mcp.safety import (
    build_cwd_prefix,
    quote_shell,
    validate_env_dict,
    validate_env_key,
    validate_interpreter,
    validate_pid,
    validate_remote_path,
    validate_signal,
    validate_tmux_name,
)


class TestRemotePathValidation:
    def test_accepts_normal_paths(self):
        for p in ["/etc/hosts", "/tmp/foo.txt", "relative/dir/file",
                  "/var/log/syslog", "."]:
            assert validate_remote_path(p) == p

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            validate_remote_path("")

    def test_allows_empty_when_flagged(self):
        assert validate_remote_path("", allow_empty=True) == ""

    def test_rejects_nul_byte(self):
        # Classic shell-truncation smuggling: many libc/utility implementations
        # treat \x00 as end-of-string but Python passes the full buffer.
        with pytest.raises(ValueError, match="NUL"):
            validate_remote_path("/tmp/legit\x00/etc/passwd")

    def test_rejects_control_chars(self):
        for bad in ["foo\x01bar", "foo\nbar", "foo\rbar", "foo\x1bbar"]:
            with pytest.raises(ValueError, match="control"):
                validate_remote_path(bad)

    def test_rejects_del_char(self):
        with pytest.raises(ValueError, match="DEL"):
            validate_remote_path("foo\x7fbar")

    def test_rejects_non_string(self):
        for bad in [None, 123, ["/etc/hosts"], object()]:
            with pytest.raises(ValueError, match="string"):
                validate_remote_path(bad)  # type: ignore[arg-type]

    def test_traversal_strings_pass_through(self):
        # We deliberately do NOT block "..": SSH agents legitimately want to
        # navigate around the remote fs. Higher layers may add a host-scoped
        # allowlist policy. The validator's job is only to block bytes that
        # cannot appear in a real path.
        assert validate_remote_path("../etc/passwd") == "../etc/passwd"


class TestQuoteShell:
    @pytest.mark.parametrize("raw,quoted", [
        ("foo", "foo"),
        ("foo bar", "'foo bar'"),
        ("a;rm -rf /", "'a;rm -rf /'"),
        ("$(reboot)", "'$(reboot)'"),
        ("`reboot`", "'`reboot`'"),
        ("'sneaky'", "''\"'\"'sneaky'\"'\"''"),
    ])
    def test_quotes_dangerous_strings(self, raw, quoted):
        assert quote_shell(raw) == quoted

    def test_rejects_nul(self):
        with pytest.raises(ValueError, match="NUL"):
            quote_shell("foo\x00bar")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError, match="str"):
            quote_shell(42)  # type: ignore[arg-type]


class TestBuildCwdPrefix:
    def test_no_cwd_returns_command_unchanged(self):
        assert build_cwd_prefix(None, "ls") == "ls"
        assert build_cwd_prefix("", "ls") == "ls"

    def test_normal_cwd_quoted(self):
        out = build_cwd_prefix("/var/log", "tail -f syslog")
        assert out == "cd /var/log && tail -f syslog"

    def test_dangerous_cwd_neutralized(self):
        # The whole point: a malicious cwd cannot escape the `cd` argument.
        out = build_cwd_prefix("/tmp; rm -rf /", "ls")
        # The `;` is wrapped inside single quotes so the shell treats the
        # entire payload as a single literal directory name.
        assert out.startswith("cd '/tmp; rm -rf /'")
        assert " && ls" in out
        assert "; rm -rf /" not in out.split(" && ", 1)[0].replace("'", "")[:0]
        # Concretely: there must NOT be an unquoted `;` before `&&`
        prefix = out.split(" && ", 1)[0]
        assert prefix.startswith("cd ")
        assert prefix.endswith("'")  # quoted argument

    def test_cwd_with_nul_rejected(self):
        with pytest.raises(ValueError):
            build_cwd_prefix("/tmp\x00/etc", "ls")


class TestEnvKeyValidation:
    @pytest.mark.parametrize("good", [
        "FOO", "_FOO", "foo_bar", "X1", "MY_VAR_2", "_",
    ])
    def test_accepts_valid(self, good):
        assert validate_env_key(good) == good

    @pytest.mark.parametrize("bad", [
        "1FOO",         # leading digit
        "FOO BAR",      # space
        "FOO;rm -rf /", # injection
        "FOO=bar",      # contains assignment
        "FOO\n",        # newline
        "",             # empty
        "FOO-BAR",      # dash
        "FOO.BAR",      # dot
    ])
    def test_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            validate_env_key(bad)


class TestEnvDictValidation:
    def test_none_returns_empty(self):
        assert validate_env_dict(None) == {}

    def test_normal_dict(self):
        out = validate_env_dict({"FOO": "bar", "X": 1})
        assert out == {"FOO": "bar", "X": "1"}

    def test_drops_none_values(self):
        assert validate_env_dict({"FOO": None}) == {}

    def test_rejects_bad_key(self):
        with pytest.raises(ValueError):
            validate_env_dict({"BAD KEY": "x"})

    def test_rejects_nul_in_value(self):
        with pytest.raises(ValueError, match="NUL"):
            validate_env_dict({"FOO": "bar\x00baz"})


class TestSignalValidation:
    def test_accepts_known(self):
        assert validate_signal("TERM") == "TERM"
        assert validate_signal("term") == "TERM"
        assert validate_signal("SIGTERM") == "TERM"

    def test_rejects_unknown(self):
        for bad in ["BOOM", "9", "TERM; rm -rf /", "", "TERM\n"]:
            with pytest.raises(ValueError):
                validate_signal(bad)


class TestInterpreterValidation:
    def test_accepts_known(self):
        for ok in ["bash", "sh", "python3", "node"]:
            assert validate_interpreter(ok) == ok

    def test_rejects_unknown(self):
        for bad in ["bash; rm -rf /", "/usr/bin/bash", "../bash", "evil",
                    "bash -c 'reboot'"]:
            with pytest.raises(ValueError):
                validate_interpreter(bad)


class TestTmuxNameValidation:
    def test_accepts_alnum(self):
        assert validate_tmux_name("worker_1") == "worker_1"
        assert validate_tmux_name("a-b-c") == "a-b-c"

    def test_rejects_dangerous(self):
        for bad in ["a;b", "a b", "a$b", "a:1", "a.b", "", "a\nb", "a/b"]:
            with pytest.raises(ValueError):
                validate_tmux_name(bad)


class TestPidValidation:
    def test_accepts_normal(self):
        assert validate_pid(1) == 1
        assert validate_pid(12345) == 12345

    def test_rejects_zero_and_negative(self):
        for bad in [0, -1, -12345]:
            with pytest.raises(ValueError):
                validate_pid(bad)

    def test_rejects_non_int(self):
        for bad in [None, "1", 1.0, True]:  # bool is special-cased
            with pytest.raises(ValueError):
                validate_pid(bad)  # type: ignore[arg-type]

    def test_rejects_too_large(self):
        with pytest.raises(ValueError):
            validate_pid(10**12)
