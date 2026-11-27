"""Tests for persistent ControlMaster setup (`onirika persist`) and clean-up (`onirika clean-up`)."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from onirika import cli, control_master as cm


def _make_unix_socket(path: Path) -> Path:
    """Create a real (unbound/dead) Unix domain socket file on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()
    return path


@pytest.fixture
def listen():
    """Factory for listening Unix sockets. ``listen.servers`` maps path -> socket."""
    servers: dict[str, socket.socket] = {}

    def _listen(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(path))
        s.listen(64)
        servers[str(path)] = s
        return path

    _listen.servers = servers
    yield _listen
    for s in servers.values():
        s.close()


def _write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(content)
    return p


class TestHelpers:
    def test_parse_master_pid(self):
        assert cm.parse_master_pid("Master running (pid=42424)") == 42424
        assert cm.parse_master_pid("Master running (pid=7)\n") == 7
        assert cm.parse_master_pid("Control socket connect: No such file") is None

    def test_build_default_socket_path_short(self):
        ssh_dir = Path("/tmp/onr")
        p = cm.build_default_socket_path("box.local", "alice", 22, ssh_dir=ssh_dir)
        assert Path(p).parent == ssh_dir
        assert Path(p).name == "onirika-ctrl-alice@box.local:22"

    def test_build_default_socket_path_long_hashes(self, tmp_path):
        ssh_dir = tmp_path / "ssh"
        long_host = "very-long-compute-node-name-" + "x" * 90 + ".example.org"
        p1 = cm.build_default_socket_path(long_host, "alice", 2222, ssh_dir=ssh_dir)
        p2 = cm.build_default_socket_path(long_host, "alice", 2222, ssh_dir=ssh_dir)
        assert p1 == p2
        assert Path(p1).name.startswith(cm.ONIRIKA_SOCKET_PREFIX)
        assert len(Path(p1).name) == len(cm.ONIRIKA_SOCKET_PREFIX) + 16

    def test_resolve_ssh_config_parses_output(self, monkeypatch, tmp_path):
        fake_cp = str(tmp_path / "cm-alice@srv.example.com:2222")
        fake_stdout = (
            "user alice\n"
            "hostname srv.example.com\n"
            "port 2222\n"
            f"controlpath {fake_cp}\n"
        )
        monkeypatch.setattr(
            cm.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout=fake_stdout, stderr=""),
        )
        res = cm.resolve_ssh_config("myalias")
        assert res.user == "alice"
        assert res.hostname == "srv.example.com"
        assert res.port == 2222
        assert res.control_path == os.path.abspath(fake_cp)

    def test_resolve_ssh_config_handles_none_controlpath(self, monkeypatch):
        fake_stdout = "user bob\nhostname host.example.com\nport 22\ncontrolpath none\n"
        monkeypatch.setattr(
            cm.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout=fake_stdout, stderr=""),
        )
        res = cm.resolve_ssh_config("bob@host.example.com")
        assert res.user == "bob"
        assert res.hostname == "host.example.com"
        assert res.port == 22
        assert res.control_path is None

    def test_resolve_ssh_config_expands_leftover_tokens(self, monkeypatch, tmp_path):
        template = str(tmp_path / "ctrl-%r@%h:%p")
        fake_stdout = f"user bob\nhostname real.host\nport 2200\ncontrolpath {template}\n"
        monkeypatch.setattr(
            cm.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout=fake_stdout, stderr=""),
        )
        res = cm.resolve_ssh_config("alias")
        assert res.control_path == os.path.abspath(str(tmp_path / "ctrl-bob@real.host:2200"))


class TestPersistMaster:
    def test_reuses_existing_live_master_without_spawning_ssh(self, tmp_path, monkeypatch):
        ssh_dir = tmp_path / "ssh"
        reg_path = tmp_path / "data" / "masters.json"
        existing_sock = _make_unix_socket(ssh_dir / "existing.sock")

        monkeypatch.setattr(
            cm,
            "resolve_ssh_config",
            lambda target, port=None, control_path_override=None: cm.SSHResolvedConfig(
                hostname="srv.example.com",
                user="alice",
                port=22,
                control_path=str(existing_sock),
            ),
        )
        monkeypatch.setattr(
            cm,
            "check_master",
            lambda path, target=None, port=None, timeout=5.0: (
                (True, 9999, "Master running (pid=9999)")
                if path == str(existing_sock)
                else (False, None, "not running")
            ),
        )
        run_mock = MagicMock()
        monkeypatch.setattr(cm.subprocess, "run", run_mock)

        # Ad-hoc hostname with no onirika config file
        monkeypatch.setenv("ONIRIKA_CONFIG", str(tmp_path / "no-config.yaml"))

        result = cm.persist_master(
            "alice@srv.example.com",
            registry_path=reg_path,
            ssh_dir=ssh_dir,
        )

        assert result.reused is True
        assert result.control_path == str(existing_sock)
        assert result.pid == 9999
        run_mock.assert_not_called()
        # Reusing a foreign (non-Onirika) socket should not adopt it into masters.json.
        assert not reg_path.exists()

    def test_creates_new_master_for_adhoc_host_with_keepalive(self, tmp_path, monkeypatch):
        ssh_dir = tmp_path / "ssh"
        reg_path = tmp_path / "data" / "masters.json"
        monkeypatch.setenv("ONIRIKA_CONFIG", str(tmp_path / "no-config.yaml"))

        monkeypatch.setattr(
            cm,
            "resolve_ssh_config",
            lambda target, port=None, control_path_override=None: cm.SSHResolvedConfig(
                hostname="srv.example.com",
                user="alice",
                port=22,
                control_path=None,
            ),
        )

        launched_argv: list[str] = []
        created_sock: list[str] = []

        def fake_run(argv, **kwargs):
            launched_argv[:] = argv
            sock_idx = argv.index("-S") + 1
            sock_path = Path(argv[sock_idx])
            _make_unix_socket(sock_path)
            created_sock.append(str(sock_path))
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(cm.subprocess, "run", fake_run)
        monkeypatch.setattr(
            cm,
            "check_master",
            lambda path, target=None, port=None, timeout=5.0: (
                (True, 54321, "Master running (pid=54321)")
                if path in created_sock
                else (False, None, "no socket")
            ),
        )

        result = cm.persist_master(
            "alice@srv.example.com",
            interval=30,
            count_max=4,
            registry_path=reg_path,
            ssh_dir=ssh_dir,
        )

        expected_path = cm.build_default_socket_path(
            "srv.example.com", "alice", 22, ssh_dir=ssh_dir
        )
        assert result.reused is False
        assert result.pid == 54321
        assert result.interval == 30
        assert result.count_max == 4
        assert result.control_path == expected_path
        assert Path(result.control_path).parent == ssh_dir
        assert Path(result.control_path).name.startswith(cm.ONIRIKA_SOCKET_PREFIX)

        # Verify SSH command flags for keep-alive pulse and background persistence
        assert launched_argv[0] == "ssh"
        assert "-M" in launched_argv
        assert "-f" in launched_argv
        assert "-N" in launched_argv
        assert "ControlMaster=yes" in launched_argv
        assert "ControlPersist=yes" in launched_argv
        assert "ServerAliveInterval=30" in launched_argv
        assert "ServerAliveCountMax=4" in launched_argv
        assert launched_argv[-1] == "alice@srv.example.com"

        # Verify registry recorded with 0o600 permissions
        assert reg_path.exists()
        assert stat.S_IMODE(os.stat(reg_path).st_mode) == 0o600
        reg_data = json.loads(reg_path.read_text())
        assert result.control_path in reg_data["masters"]
        entry = reg_data["masters"][result.control_path]
        assert entry["pid"] == 54321
        assert entry["server_alive_interval"] == 30
        assert entry["server_alive_count_max"] == 4

    def test_removes_stale_socket_before_creating_new_master(self, tmp_path, monkeypatch):
        ssh_dir = tmp_path / "ssh"
        reg_path = tmp_path / "data" / "masters.json"
        expected_path = Path(
            cm.build_default_socket_path("srv.example.com", "alice", 22, ssh_dir=ssh_dir)
        )
        stale_sock = _make_unix_socket(expected_path)
        monkeypatch.setenv("ONIRIKA_CONFIG", str(tmp_path / "no-config.yaml"))

        monkeypatch.setattr(
            cm,
            "resolve_ssh_config",
            lambda target, port=None, control_path_override=None: cm.SSHResolvedConfig(
                hostname="srv.example.com",
                user="alice",
                port=22,
                control_path=None,
            ),
        )

        socket_existed_at_launch = []
        created = False

        def fake_run(argv, **kwargs):
            nonlocal created
            sock_path = Path(argv[argv.index("-S") + 1])
            socket_existed_at_launch.append(sock_path.exists())
            _make_unix_socket(sock_path)
            created = True
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(cm.subprocess, "run", fake_run)
        monkeypatch.setattr(
            cm,
            "check_master",
            lambda path, target=None, port=None, timeout=5.0: (
                (True, 1111, "Master running (pid=1111)")
                if created
                else (False, None, "Connection refused")
            ),
        )

        result = cm.persist_master(
            "alice@srv.example.com",
            registry_path=reg_path,
            ssh_dir=ssh_dir,
        )

        # Stale socket was unlinked BEFORE ssh -M was spawned
        assert socket_existed_at_launch == [False]
        assert result.reused is False
        assert result.control_path == str(stale_sock)

    def test_refuses_to_remove_non_socket_file(self, tmp_path, monkeypatch):
        regular_file = tmp_path / "not-a-socket"
        regular_file.write_text("important data")
        monkeypatch.setenv("ONIRIKA_CONFIG", str(tmp_path / "no-config.yaml"))
        monkeypatch.setattr(
            cm,
            "resolve_ssh_config",
            lambda target, port=None, control_path_override=None: cm.SSHResolvedConfig(
                hostname="srv",
                user="alice",
                port=22,
                control_path=control_path_override,
            ),
        )
        monkeypatch.setattr(
            cm,
            "check_master",
            lambda *a, **kw: (False, None, "not a socket"),
        )

        with pytest.raises(ValueError, match="non-socket"):
            cm.persist_master(
                "alice@srv",
                control_path=str(regular_file),
                registry_path=tmp_path / "masters.json",
                ssh_dir=tmp_path / "ssh",
            )
        assert regular_file.read_text() == "important data"

    def test_uses_configured_host_and_default_host(self, tmp_path, monkeypatch):
        ssh_dir = tmp_path / "ssh"
        reg_path = tmp_path / "data" / "masters.json"
        ctrl_template = str(ssh_dir / "ctrl-%r@%h:%p")
        cfg_file = _write_config(
            tmp_path,
            f"""
default_host: prod
hosts:
  prod:
    ssh_host: prod.internal
    user: deploy
    port: 2222
    control_path: "{ctrl_template}"
""",
        )

        expected_sock = str(ssh_dir / "ctrl-deploy@prod.internal:2222")
        monkeypatch.setattr(
            cm,
            "resolve_ssh_config",
            lambda target, port=None, control_path_override=None: cm.SSHResolvedConfig(
                hostname="prod.internal",
                user="deploy",
                port=2222,
                control_path=control_path_override or None,
            ),
        )

        launched: list[str] = []
        created = False

        def fake_run(argv, **kwargs):
            nonlocal created
            launched[:] = argv
            _make_unix_socket(Path(argv[argv.index("-S") + 1]))
            created = True
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(cm.subprocess, "run", fake_run)
        monkeypatch.setattr(
            cm,
            "check_master",
            lambda path, target=None, port=None, timeout=5.0: (
                (True, 777, "Master running (pid=777)")
                if created
                else (False, None, "not running")
            ),
        )

        # Omit hostname -> uses default_host ("prod")
        result = cm.persist_master(
            None,
            config_path=cfg_file,
            registry_path=reg_path,
            ssh_dir=ssh_dir,
        )

        assert result.display_name == "prod"
        assert result.target == "deploy@prod.internal"
        assert result.control_path == expected_sock
        assert "-p" in launched and "2222" in launched

    def test_ssh_failure_raises_and_does_not_register(self, tmp_path, monkeypatch):
        reg_path = tmp_path / "data" / "masters.json"
        monkeypatch.setenv("ONIRIKA_CONFIG", str(tmp_path / "no-config.yaml"))
        monkeypatch.setattr(
            cm,
            "resolve_ssh_config",
            lambda target, port=None, control_path_override=None: cm.SSHResolvedConfig(
                hostname="bad.host", user="alice", port=22, control_path=None
            ),
        )
        monkeypatch.setattr(
            cm.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 255),
        )

        with pytest.raises(RuntimeError, match="ssh exit 255"):
            cm.persist_master(
                "alice@bad.host",
                registry_path=reg_path,
                ssh_dir=tmp_path / "ssh",
            )
        assert not reg_path.exists()

    def test_invalid_interval_or_count_raises(self):
        with pytest.raises(ValueError, match="--interval"):
            cm.persist_master("example.com", interval=0)
        with pytest.raises(ValueError, match="--count-max"):
            cm.persist_master("example.com", count_max=-1)


class TestCleanupMasters:
    def test_cleans_stale_and_leaves_active_without_force(self, tmp_path, monkeypatch, listen):
        ssh_dir = tmp_path / "ssh"
        reg_path = tmp_path / "data" / "masters.json"
        monkeypatch.setenv("ONIRIKA_CONFIG", str(tmp_path / "no-config.yaml"))

        stale_registered = _make_unix_socket(ssh_dir / "onirika-ctrl-stale@host1:22")
        active_registered = listen(ssh_dir / "onirika-ctrl-live@host2:22")
        orphan_stale = _make_unix_socket(ssh_dir / "onirika-ctrl-orphan@host3:22")
        unrelated_sock = _make_unix_socket(ssh_dir / "someone-else.sock")
        missing_path = str(ssh_dir / "onirika-ctrl-gone@host4:22")

        cm._save_registry(
            reg_path,
            {
                str(stale_registered): {"control_path": str(stale_registered), "target": "stale@host1", "port": 22},
                str(active_registered): {"control_path": str(active_registered), "target": "live@host2", "port": 22},
                missing_path: {"control_path": missing_path, "target": "gone@host4", "port": 22},
            },
        )

        def fake_check(path, target=None, port=None, timeout=5.0):
            if path == str(active_registered):
                return True, 2020, "Master running (pid=2020)"
            return False, None, "Connection refused"

        exit_mock = MagicMock()
        monkeypatch.setattr(cm, "check_master", fake_check)
        monkeypatch.setattr(cm, "exit_master", exit_mock)

        items = cm.cleanup_masters(
            force=False,
            registry_path=reg_path,
            ssh_dir=ssh_dir,
        )

        by_path = {it.control_path: it for it in items}
        assert by_path[str(stale_registered)].status == "removed_stale"
        assert by_path[str(orphan_stale)].status == "removed_stale"
        assert by_path[missing_path].status == "pruned_missing"
        assert by_path[str(active_registered)].status == "kept_active"
        assert by_path[str(active_registered)].pid == 2020
        assert str(unrelated_sock) not in by_path

        # Stale sockets deleted; active and unrelated sockets untouched
        assert not stale_registered.exists()
        assert not orphan_stale.exists()
        assert active_registered.exists()
        assert unrelated_sock.exists()
        exit_mock.assert_not_called()

        # Registry keeps only the still-active entry
        saved = cm._load_registry(reg_path)
        assert list(saved.keys()) == [str(active_registered)]

    def test_force_closes_only_prefixed_masters(self, tmp_path, monkeypatch, listen):
        ssh_dir = tmp_path / "ssh"
        reg_path = tmp_path / "data" / "masters.json"

        cfg_sock = listen(ssh_dir / "ctrl-alice@prod.example.com:22")
        cfg_file = _write_config(
            tmp_path,
            f"""
default_host: prod
hosts:
  prod:
    ssh_host: prod.example.com
    user: alice
    control_path: "{ssh_dir}/ctrl-%r@%h:%p"
""",
        )

        active_persist = listen(ssh_dir / "onirika-ctrl-bob@adhoc:22")
        # In the registry, but without the prefix (e.g. --control-path).
        custom_sock = listen(ssh_dir / "custom.sock")
        stale_persist = _make_unix_socket(ssh_dir / "onirika-ctrl-dead@adhoc:22")
        unrelated_sock = _make_unix_socket(ssh_dir / "non-onirika.sock")

        cm._save_registry(
            reg_path,
            {
                str(active_persist): {"control_path": str(active_persist), "target": "bob@adhoc", "port": 22},
                str(stale_persist): {"control_path": str(stale_persist), "target": "dead@adhoc", "port": 22},
                str(custom_sock): {"control_path": str(custom_sock), "target": "me@custom", "port": 22},
            },
        )

        monkeypatch.setattr(
            cm,
            "resolve_ssh_config",
            lambda target, port=None, control_path_override=None: cm.SSHResolvedConfig(
                hostname="prod.example.com",
                user="alice",
                port=22,
                control_path=str(cfg_sock),
            ),
        )

        def fake_check(path, target=None, port=None, timeout=5.0):
            return True, 3333, "Master running (pid=3333)"

        exited_paths: list[str] = []

        def fake_exit(path, target=None, port=None, timeout=5.0):
            exited_paths.append(path)
            listen.servers[path].close()
            return True, "Exit request sent."

        monkeypatch.setattr(cm, "check_master", fake_check)
        monkeypatch.setattr(cm, "exit_master", fake_exit)

        items = cm.cleanup_masters(
            force=True,
            config_path=cfg_file,
            registry_path=reg_path,
            ssh_dir=ssh_dir,
        )

        by_path = {it.control_path: it for it in items}
        assert by_path[str(active_persist)].status == "closed_active"
        assert by_path[str(cfg_sock)].status == "kept_active"
        assert by_path[str(custom_sock)].status == "kept_active"
        assert by_path[str(stale_persist)].status == "removed_stale"
        assert str(unrelated_sock) not in by_path

        assert exited_paths == [str(active_persist)]
        assert not active_persist.exists()
        assert cfg_sock.exists()
        assert custom_sock.exists()
        assert not stale_persist.exists()
        assert unrelated_sock.exists()
        assert list(cm._load_registry(reg_path)) == [str(custom_sock)]

    @pytest.mark.parametrize(
        "check_msg",
        ["ControlMaster check timed out", "ssh binary not found", "Bad configuration option"],
    )
    def test_failed_check_keeps_live_socket(self, tmp_path, monkeypatch, listen, check_msg):
        monkeypatch.setenv("ONIRIKA_CONFIG", str(tmp_path / "no-config.yaml"))
        ssh_dir = tmp_path / "ssh"
        sock = listen(ssh_dir / "onirika-ctrl-a@h:22")
        monkeypatch.setattr(cm, "check_master", lambda *a, **kw: (False, None, check_msg))

        items = cm.cleanup_masters(registry_path=tmp_path / "m.json", ssh_dir=ssh_dir)

        assert [it.status for it in items] == ["kept_active"]
        assert sock.exists()

    def test_unknown_probe_keeps_socket_and_registry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ONIRIKA_CONFIG", str(tmp_path / "no-config.yaml"))
        reg_path = tmp_path / "m.json"
        sock = _make_unix_socket(tmp_path / "ssh" / "onirika-ctrl-a@h:22")
        cm._save_registry(reg_path, {str(sock): {"target": "a@h", "port": 22}})
        monkeypatch.setattr(cm, "probe_socket", lambda *a, **kw: ("unknown", "timed out"))

        items = cm.cleanup_masters(force=True, registry_path=reg_path, ssh_dir=sock.parent)

        assert [it.status for it in items] == ["failed"]
        assert sock.exists()
        assert str(sock) in cm._load_registry(reg_path)

    def test_force_keeps_socket_if_master_does_not_exit(self, tmp_path, monkeypatch, listen):
        monkeypatch.setenv("ONIRIKA_CONFIG", str(tmp_path / "no-config.yaml"))
        monkeypatch.setattr(cm, "_EXIT_WAIT", 0.2)
        sock = listen(tmp_path / "ssh" / "onirika-ctrl-a@h:22")
        monkeypatch.setattr(cm, "check_master", lambda *a, **kw: (False, None, "timed out"))
        monkeypatch.setattr(cm, "exit_master", lambda *a, **kw: (False, "timed out"))

        items = cm.cleanup_masters(force=True, registry_path=tmp_path / "m.json", ssh_dir=sock.parent)

        assert [it.status for it in items] == ["failed"]
        assert sock.exists()


class TestProbeSocket:
    def test_states(self, tmp_path, listen):
        assert cm.probe_socket(str(listen(tmp_path / "l")))[0] == "live"
        assert cm.probe_socket(str(_make_unix_socket(tmp_path / "d")))[0] == "stale"
        assert cm.probe_socket(str(tmp_path / "none"))[0] == "missing"
        assert cm.probe_socket(str(tmp_path / ("x" * 200)))[0] == "unknown"


class TestPersistReuse:
    REQUEST = {"host_arg": "alice@srv", "target": "alice@srv", "port": 22}

    def _setup(self, tmp_path, monkeypatch, check_ok=True):
        """Fake ssh. Returns the list of ``ssh -M`` argv that were run."""
        monkeypatch.setenv("ONIRIKA_CONFIG", str(tmp_path / "no-config.yaml"))
        monkeypatch.setattr(
            cm,
            "resolve_ssh_config",
            lambda target, port=None, control_path_override=None: cm.SSHResolvedConfig(
                hostname="srv",
                user="alice",
                port=port or 22,
                control_path=control_path_override,
            ),
        )
        runs: list[list[str]] = []
        created: set[str] = set()

        def fake_run(argv, **kwargs):
            runs.append(argv)
            path = argv[argv.index("-S") + 1]
            _make_unix_socket(Path(path))
            created.add(path)
            return subprocess.CompletedProcess(argv, 0)

        def fake_check(path, target=None, port=None, timeout=5.0):
            if check_ok or path in created:
                return True, 1, "Master running (pid=1)"
            return False, None, "timed out"

        monkeypatch.setattr(cm.subprocess, "run", fake_run)
        monkeypatch.setattr(cm, "check_master", fake_check)
        return runs

    def _persist(self, tmp_path, **kw):
        return cm.persist_master(
            "alice@srv",
            registry_path=tmp_path / "m.json",
            ssh_dir=tmp_path / "ssh",
            **kw,
        )

    def test_reuses_entry_when_all_fields_match(self, tmp_path, monkeypatch, listen):
        runs = self._setup(tmp_path, monkeypatch)
        sock = listen(tmp_path / "old.sock")
        cm._save_registry(tmp_path / "m.json", {str(sock): dict(self.REQUEST)})

        result = self._persist(tmp_path)

        assert result.reused is True
        assert result.control_path == str(sock)
        assert runs == []

    @pytest.mark.parametrize(
        "field,value", [("host_arg", "other"), ("target", "bob@srv"), ("port", 2222)]
    )
    def test_skips_entry_when_one_field_differs(self, tmp_path, monkeypatch, listen, field, value):
        runs = self._setup(tmp_path, monkeypatch)
        sock = listen(tmp_path / "old.sock")
        cm._save_registry(tmp_path / "m.json", {str(sock): {**self.REQUEST, field: value}})

        result = self._persist(tmp_path)

        assert result.reused is False
        assert result.control_path != str(sock)
        assert len(runs) == 1
        assert sock.exists()

    @pytest.mark.parametrize(
        "field,value", [("host_arg", "other"), ("target", "bob@srv"), ("port", 2222)]
    )
    def test_mismatch_at_create_path_raises(self, tmp_path, monkeypatch, listen, field, value):
        runs = self._setup(tmp_path, monkeypatch)
        sock = listen(tmp_path / "fixed.sock")
        cm._save_registry(tmp_path / "m.json", {str(sock): {**self.REQUEST, field: value}})

        with pytest.raises(RuntimeError, match="registered for"):
            self._persist(tmp_path, control_path=str(sock))

        assert runs == []
        assert sock.exists()

    def test_failed_check_on_live_socket_raises(self, tmp_path, monkeypatch, listen):
        runs = self._setup(tmp_path, monkeypatch, check_ok=False)
        sock = listen(tmp_path / "hung.sock")

        with pytest.raises(RuntimeError, match="ssh -O check failed"):
            self._persist(tmp_path, control_path=str(sock))

        assert runs == []
        assert sock.exists()


class TestCLIIntegration:
    def test_cli_persist_prints_path_to_stdout_and_status_to_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cm,
            "persist_master",
            lambda hostname, **kw: cm.PersistResult(
                control_path="/tmp/onirika-ctrl-alice@box:22",
                target="alice@box",
                display_name="box",
                reused=False,
                pid=1234,
                interval=60,
                count_max=3,
            ),
        )
        monkeypatch.setattr("sys.argv", ["onirika", "persist", "box"])
        cli.main()
        captured = capsys.readouterr()
        assert captured.out.strip() == "/tmp/onirika-ctrl-alice@box:22"
        assert "Opened ControlMaster for box" in captured.err
        assert "pid=1234" in captured.err
        assert "60s x 3" in captured.err

    def test_cli_persist_quiet_suppresses_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cm,
            "persist_master",
            lambda hostname, **kw: cm.PersistResult(
                control_path="/tmp/onirika-ctrl-alice@box:22",
                target="alice@box",
                display_name="box",
                reused=True,
                pid=1234,
                interval=60,
                count_max=3,
            ),
        )
        monkeypatch.setattr("sys.argv", ["onirika", "persist", "-q", "box"])
        cli.main()
        captured = capsys.readouterr()
        assert captured.out.strip() == "/tmp/onirika-ctrl-alice@box:22"
        assert captured.err == ""

    def test_cli_persist_rejects_bad_interval(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["onirika", "persist", "box", "--interval", "0"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        assert "positive integer" in capsys.readouterr().err

    def test_cli_cleanup_prints_summary_and_force_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cm,
            "cleanup_masters",
            lambda force=False, config_path=None: [
                cm.CleanupItem("/tmp/s1", "a@b", "removed_stale"),
                cm.CleanupItem("/tmp/onirika-ctrl-s2", "c@d", "kept_active", pid=888),
            ],
        )
        monkeypatch.setattr("sys.argv", ["onirika", "clean-up"])
        cli.main()
        out = capsys.readouterr().out
        assert "Removed stale ControlMaster: /tmp/s1" in out
        assert "Active ControlMaster left running: /tmp/onirika-ctrl-s2 (pid=888)" in out
        assert "1 stale removed, 1 active left running" in out
        assert "onirika clean-up --force" in out

    def test_cli_cleanup_no_force_hint_without_prefix(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cm,
            "cleanup_masters",
            lambda **kw: [cm.CleanupItem("/tmp/ctrl-a@b:22", "a@b", "kept_active")],
        )
        monkeypatch.setattr("sys.argv", ["onirika", "clean-up"])
        cli.main()
        assert "clean-up --force" not in capsys.readouterr().out

    def test_cli_cleanup_force_flag_forwarded(self, monkeypatch, capsys):
        seen_force = []

        def fake_cleanup(force=False, config_path=None):
            seen_force.append(force)
            return [cm.CleanupItem("/tmp/s2", "c@d", "closed_active", pid=888)]

        monkeypatch.setattr(cm, "cleanup_masters", fake_cleanup)
        monkeypatch.setattr("sys.argv", ["onirika", "clean-up", "--force"])
        cli.main()
        assert seen_force == [True]
        out = capsys.readouterr().out
        assert "Closed active ControlMaster: /tmp/s2 (pid=888)" in out
        assert "onirika clean-up --force" not in out

    def test_cli_cleanup_empty(self, monkeypatch, capsys):
        monkeypatch.setattr(cm, "cleanup_masters", lambda **kw: [])
        monkeypatch.setattr("sys.argv", ["onirika", "clean-up"])
        cli.main()
        assert "No Onirika control masters found." in capsys.readouterr().out

    def test_cli_help_lists_persist_and_cleanup(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["onirika", "help"])
        cli.main()
        out = capsys.readouterr().out
        assert "onirika persist" in out
        assert "onirika clean-up" in out
