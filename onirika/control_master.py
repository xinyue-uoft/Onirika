"""Lightweight persistent SSH ControlMaster setup and clean-up.

Provides two core operations exposed through the ``onirika`` CLI:

* ``persist_master`` (``onirika persist [<hostname>]``):
  Reuses an existing live ControlMaster for the target if one is already up;
  otherwise opens a new background master with an SSH keep-alive pulse
  (``ServerAliveInterval`` / ``ServerAliveCountMax`` + ``ControlPersist=yes``),
  records it in Onirika's local registry, and returns the socket path.
  Works both for hosts configured in ``~/.config/onirika/config.yaml`` and for
  ad-hoc hostnames / ``~/.ssh/config`` aliases without requiring prior setup.

* ``cleanup_masters`` (``onirika clean-up [--force]``):
  Scans sockets from the local registry, ``~/.ssh/onirika-ctrl-*``, and
  configured hosts in ``config.yaml``. Removes stale sockets. With
  ``force=True``, also closes live masters whose socket name starts with
  ``onirika-ctrl-``.

A socket is stale only if ``connect()`` is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from onirika.config import HostConfig, load_config
from onirika.proxy_key import default_key_dir

DEFAULT_SERVER_ALIVE_INTERVAL = 60
DEFAULT_SERVER_ALIVE_COUNT_MAX = 3
DEFAULT_CHECK_TIMEOUT = 5.0
# Max wait for a master to close its socket after `ssh -O exit`.
_EXIT_WAIT = 2.0

# Prefix for ad-hoc sockets created by `onirika persist` when neither
# config.yaml nor ~/.ssh/config specifies a ControlPath. `clean-up --force`
# closes only sockets with this prefix.
ONIRIKA_SOCKET_PREFIX = "onirika-ctrl-"

# OpenSSH appends a 16-char random suffix during muxserver_listen() bind, and
# macOS sun_path is 104 bytes (Linux 108), so keep socket paths <= 85 bytes.
_MAX_SOCKET_PATH_LEN = 85

_PID_RE = re.compile(r"\(pid=(\d+)\)")


@dataclass(frozen=True)
class SSHResolvedConfig:
    """Subset of resolved OpenSSH settings parsed from ``ssh -G``."""

    hostname: str
    user: str | None = None
    port: int = 22
    control_path: str | None = None


@dataclass(frozen=True)
class PersistResult:
    """Outcome of ``persist_master``."""

    control_path: str
    target: str
    display_name: str
    reused: bool
    pid: int | None
    interval: int
    count_max: int


@dataclass(frozen=True)
class CleanupItem:
    """Outcome for a single ControlMaster socket during ``cleanup_masters``."""

    control_path: str
    target: str | None
    status: str  # "removed_stale" | "pruned_missing" | "closed_active" | "kept_active" | "failed"
    pid: int | None = None
    detail: str = ""


def default_registry_path() -> Path:
    """Return path to the Onirika ControlMaster registry file."""
    return default_key_dir() / "masters.json"


def default_ssh_dir() -> Path:
    """Return path to the user's ~/.ssh directory."""
    return Path.home() / ".ssh"


def _load_registry(registry_path: Path) -> dict[str, dict]:
    """Load the ControlMaster registry mapping socket_path -> metadata."""
    if not registry_path.exists():
        return {}
    try:
        data = json.loads(registry_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    masters = data.get("masters") if isinstance(data, dict) else None
    if not isinstance(masters, dict):
        return {}
    return {
        str(k): v
        for k, v in masters.items()
        if isinstance(k, str) and isinstance(v, dict)
    }


def _save_registry(registry_path: Path, masters: dict[str, dict]) -> None:
    """Atomically write the ControlMaster registry with 0o600 permissions."""
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(registry_path.parent, 0o700)

    tmp = registry_path.with_suffix(".json.tmp")
    payload = json.dumps({"masters": masters}, indent=2, sort_keys=True) + "\n"
    tmp.write_text(payload)
    os.chmod(tmp, 0o600)
    os.replace(tmp, registry_path)


def _normalize_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _expand_tokens(
    template: str,
    *,
    hostname: str,
    user: str | None,
    port: int,
    original_host: str,
) -> str:
    """Expand ~ and common OpenSSH ControlPath tokens if ssh -G left any unexpanded."""
    path = os.path.expanduser(template)
    if "%C" in path:
        local_host = socket.gethostname()
        raw = f"{local_host}{hostname}{port}{user or ''}"
        digest = hashlib.sha1(raw.encode()).hexdigest()
        path = path.replace("%C", digest)
    path = path.replace("%h", hostname)
    path = path.replace("%n", original_host)
    path = path.replace("%p", str(port))
    if user:
        path = path.replace("%r", user)
    return path


def resolve_ssh_config(
    target: str,
    port: int | None = None,
    control_path_override: str | None = None,
) -> SSHResolvedConfig:
    """Query ``ssh -G`` for OpenSSH's effective config for *target*.

    Falls back gracefully if ``ssh -G`` fails or is unavailable.
    """
    argv = ["ssh", "-G"]
    if port is not None:
        argv += ["-p", str(port)]
    if control_path_override:
        argv += ["-o", f"ControlPath={control_path_override}"]
    argv.append(target)

    # Extract fallback user/host from target string like "alice@host"
    fallback_user: str | None = None
    fallback_host = target
    if "@" in target:
        fallback_user, fallback_host = target.split("@", 1)

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=DEFAULT_CHECK_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        cp = (
            _expand_tokens(
                control_path_override,
                hostname=fallback_host,
                user=fallback_user,
                port=port or 22,
                original_host=fallback_host,
            )
            if control_path_override
            else None
        )
        return SSHResolvedConfig(
            hostname=fallback_host,
            user=fallback_user,
            port=port or 22,
            control_path=cp,
        )

    if proc.returncode != 0:
        cp = (
            _expand_tokens(
                control_path_override,
                hostname=fallback_host,
                user=fallback_user,
                port=port or 22,
                original_host=fallback_host,
            )
            if control_path_override
            else None
        )
        return SSHResolvedConfig(
            hostname=fallback_host,
            user=fallback_user,
            port=port or 22,
            control_path=cp,
        )

    parsed: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            key, val = parts[0].lower(), parts[1].strip()
            if key not in parsed:
                parsed[key] = val

    hostname = parsed.get("hostname", fallback_host)
    user = parsed.get("user", fallback_user)
    try:
        resolved_port = int(parsed.get("port", str(port or 22)))
    except ValueError:
        resolved_port = port or 22

    raw_cp = parsed.get("controlpath")
    control_path: str | None = None
    if raw_cp and raw_cp.lower() != "none":
        control_path = _normalize_path(
            _expand_tokens(
                raw_cp,
                hostname=hostname,
                user=user,
                port=resolved_port,
                original_host=fallback_host,
            )
        )

    return SSHResolvedConfig(
        hostname=hostname,
        user=user,
        port=resolved_port,
        control_path=control_path,
    )


def build_default_socket_path(
    hostname: str,
    user: str | None,
    port: int,
    ssh_dir: Path | None = None,
) -> str:
    """Build Onirika's dedicated socket path for an ad-hoc persisted host.

    Uses a human-readable ``~/.ssh/onirika-ctrl-<user>@<host>:<port>`` name
    when it fits safely within Unix domain socket length limits, otherwise
    falls back to a compact deterministic hash.
    """
    base = ssh_dir or default_ssh_dir()
    safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", hostname)
    safe_user = re.sub(r"[^A-Za-z0-9._-]", "_", user) if user else ""
    identity = f"{safe_user}@{safe_host}:{port}" if safe_user else f"{safe_host}:{port}"
    candidate = str(base / f"{ONIRIKA_SOCKET_PREFIX}{identity}")
    if len(candidate.encode("utf-8")) <= _MAX_SOCKET_PATH_LEN:
        return _normalize_path(candidate)

    digest = hashlib.sha256(f"{user or ''}@{hostname}:{port}".encode()).hexdigest()[:16]
    return _normalize_path(str(base / f"{ONIRIKA_SOCKET_PREFIX}{digest}"))


def parse_master_pid(output: str) -> int | None:
    """Extract the master PID from ``ssh -O check`` output, if present."""
    match = _PID_RE.search(output)
    return int(match.group(1)) if match else None


def check_master(
    control_path: str,
    target: str | None = None,
    port: int | None = None,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
) -> tuple[bool, int | None, str]:
    """Check whether a ControlMaster socket is currently live.

    Returns ``(is_alive, pid, raw_output)``.
    """
    argv = ["ssh", "-O", "check", "-S", control_path]
    if port is not None and port != 22:
        argv += ["-p", str(port)]
    argv.append(target or "_")

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, None, "ControlMaster check timed out"
    except FileNotFoundError:
        return False, None, "ssh binary not found"

    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        return True, parse_master_pid(output), output
    return False, None, output


def exit_master(
    control_path: str,
    target: str | None = None,
    port: int | None = None,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
) -> tuple[bool, str]:
    """Ask a live ControlMaster to exit via ``ssh -O exit``."""
    argv = ["ssh", "-O", "exit", "-S", control_path]
    if port is not None and port != 22:
        argv += ["-p", str(port)]
    argv.append(target or "_")

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "ControlMaster exit timed out"
    except FileNotFoundError:
        return False, "ssh binary not found"

    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output


def _is_unix_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        return False


def probe_socket(path: str, timeout: float = DEFAULT_CHECK_TIMEOUT) -> tuple[str, str]:
    """Probe a Unix socket with ``connect()``.

    Returns ``(state, error)``. *state* is "live", "stale" (connection refused),
    "missing", or "unknown" (any other error).
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(path)
        except ConnectionRefusedError as e:
            return "stale", str(e)
        except FileNotFoundError as e:
            return "missing", str(e)
        except OSError as e:
            return "unknown", str(e)
    return "live", ""


def _probe_after_exit(path: str) -> str:
    """Probe until the socket is no longer live or ``_EXIT_WAIT`` expires."""
    deadline = time.monotonic() + _EXIT_WAIT
    while True:
        state, _ = probe_socket(path)
        if state != "live" or time.monotonic() >= deadline:
            return state
        time.sleep(0.1)


def _remove_stale_socket_file(path: str) -> bool:
    """Unlink a dead Unix domain socket file. Refuses to delete non-sockets."""
    if not os.path.lexists(path):
        return False
    if not _is_unix_socket(path):
        raise ValueError(
            f"Refusing to remove non-socket path at ControlPath: {path}"
        )
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def _try_load_host_config(
    hostname: str | None,
    config_path: Path | None,
) -> tuple[str | None, HostConfig | None]:
    """Look up *hostname* in Onirika's config.yaml if present.

    Returns ``(alias, HostConfig)`` if found, or ``(None, None)`` when
    *hostname* is an ad-hoc SSH target not in Onirika's config (or when no
    config file exists). Raises ``FileNotFoundError`` / ``ValueError`` when
    *hostname* is omitted and config cannot supply a default host, or when an
    explicit ``--config`` file does not exist.
    """
    try:
        cfg = load_config(config_path)
    except FileNotFoundError:
        if hostname is None or config_path is not None:
            raise
        return None, None

    if hostname is None:
        alias = cfg.default_host
        if not alias:
            if len(cfg.hosts) == 1:
                alias = next(iter(cfg.hosts.keys()))
            else:
                raise ValueError(
                    "No hostname specified and no default_host configured. "
                    f"Available hosts: {', '.join(cfg.hosts.keys())}"
                )
        return alias, cfg.get_host(alias)

    if hostname in cfg.hosts:
        return hostname, cfg.hosts[hostname]

    return None, None


def _dedupe(paths: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for p in paths:
        if not p:
            continue
        norm = _normalize_path(p)
        if norm not in seen:
            seen.add(norm)
            result.append(norm)
    return result


def _entry_matches(meta: dict, host_arg: str, target: str, port: int) -> bool:
    """True if a registry entry has the same host_arg, target, and port."""
    recorded = (meta.get("host_arg"), meta.get("target"), meta.get("port"))
    return recorded == (host_arg, target, port)


def _candidate_control_paths(
    *,
    host_arg: str,
    host_cfg: HostConfig | None,
    ssh_target: str,
    explicit_port: int | None,
    ssh_resolved: SSHResolvedConfig,
    explicit_control_path: str | None,
    registry: dict[str, dict],
    ssh_dir: Path,
) -> tuple[list[str], str]:
    """Compute (candidates_to_reuse, fallback_create_path).

    Candidates are ordered by preference for reusing an existing live master.
    """
    if explicit_control_path:
        resolved = resolve_ssh_config(
            ssh_target,
            port=explicit_port,
            control_path_override=explicit_control_path,
        )
        cp = resolved.control_path or _normalize_path(explicit_control_path)
        return [cp], cp

    candidates: list[str | None] = []
    primary_create: str | None = None

    if host_cfg is not None:
        # 1. Path that SSHExecutor / onirika proxy checks.
        exec_resolved = resolve_ssh_config(
            ssh_target,
            port=explicit_port,
            control_path_override=host_cfg.resolved_control_path,
        )
        exec_cp = exec_resolved.control_path or _normalize_path(
            host_cfg.resolved_control_path
        )
        candidates.append(exec_cp)
        primary_create = exec_cp

        # 2. Path that `onirika establish` / `onirika launch` creates (raw template).
        raw_resolved = resolve_ssh_config(
            ssh_target,
            port=explicit_port,
            control_path_override=os.path.expanduser(host_cfg.control_path),
        )
        candidates.append(raw_resolved.control_path)

    # 3. Any ControlPath configured in ~/.ssh/config for this target.
    if ssh_resolved.control_path:
        candidates.append(ssh_resolved.control_path)
        if primary_create is None:
            primary_create = ssh_resolved.control_path

    # 4. Matching entries previously recorded in Onirika's registry.
    for path, meta in registry.items():
        if _entry_matches(meta, host_arg, ssh_target, ssh_resolved.port):
            candidates.append(path)

    # 5. Onirika's dedicated ad-hoc persist socket path.
    default_cp = build_default_socket_path(
        ssh_resolved.hostname,
        ssh_resolved.user,
        ssh_resolved.port,
        ssh_dir=ssh_dir,
    )
    candidates.append(default_cp)
    if primary_create is None:
        primary_create = default_cp

    return _dedupe(candidates), _normalize_path(primary_create)


def persist_master(
    hostname: str | None = None,
    *,
    interval: int = DEFAULT_SERVER_ALIVE_INTERVAL,
    count_max: int = DEFAULT_SERVER_ALIVE_COUNT_MAX,
    control_path: str | None = None,
    port: int | None = None,
    config_path: Path | None = None,
    registry_path: Path | None = None,
    ssh_dir: Path | None = None,
) -> PersistResult:
    """Reuse an existing ControlMaster for *hostname* or open a persistent one.

    A socket in the registry is reused only if its host_arg, target, and port
    all match the request. Raises ``RuntimeError`` if a socket that cannot be
    reused occupies the new master's path and is not stale.

    When creating a new master, starts SSH in background master mode (``-M -f -N``)
    with keep-alive pulses (``ServerAliveInterval`` and ``ServerAliveCountMax``)
    and ``ControlPersist=yes``, verifies the socket is up, and records it in
    Onirika's local registry.
    """
    if interval <= 0:
        raise ValueError("--interval must be a positive integer")
    if count_max <= 0:
        raise ValueError("--count-max must be a positive integer")

    registry_path = registry_path or default_registry_path()
    ssh_dir = ssh_dir or default_ssh_dir()

    alias, host_cfg = _try_load_host_config(hostname, config_path)
    if host_cfg is not None:
        display_name = alias or hostname or host_cfg.ssh_host
        ssh_target = host_cfg.ssh_target
        effective_port = port if port is not None else (
            host_cfg.port if host_cfg.port != 22 else None
        )
    else:
        assert hostname is not None
        display_name = hostname
        ssh_target = hostname
        effective_port = port

    ssh_resolved = resolve_ssh_config(ssh_target, port=effective_port)
    registry = _load_registry(registry_path)

    candidates, create_path = _candidate_control_paths(
        host_arg=display_name,
        host_cfg=host_cfg,
        ssh_target=ssh_target,
        explicit_port=effective_port,
        ssh_resolved=ssh_resolved,
        explicit_control_path=control_path,
        registry=registry,
        ssh_dir=ssh_dir,
    )

    # Prefer reusing an existing live ControlMaster.
    refusals: dict[str, str] = {}
    for candidate in candidates:
        if not os.path.lexists(candidate):
            continue
        meta = registry.get(candidate)
        if meta is not None and not _entry_matches(
            meta, display_name, ssh_target, ssh_resolved.port
        ):
            refusals[candidate] = (
                f"registered for host_arg={meta.get('host_arg')}, "
                f"target={meta.get('target')}, port={meta.get('port')}"
            )
            continue
        alive, pid, msg = check_master(candidate, ssh_target, effective_port)
        if not alive:
            refusals[candidate] = f"ssh -O check failed: {msg}"
        else:
            # Update PID if this socket is already tracked by Onirika, without
            # adopting unrelated non-Onirika sockets into the registry.
            if candidate in registry:
                registry[candidate]["pid"] = pid
                _save_registry(registry_path, registry)
            return PersistResult(
                control_path=candidate,
                target=ssh_target,
                display_name=display_name,
                reused=True,
                pid=pid,
                interval=interval,
                count_max=count_max,
            )

    # Fallback: remove a stale socket at create_path and open a new master.
    if os.path.lexists(create_path):
        if _is_unix_socket(create_path):
            state, error = probe_socket(create_path)
            if state in ("live", "unknown"):
                reason = refusals.get(create_path) or error or "socket is in use"
                raise RuntimeError(
                    f"{state} socket at {create_path} was not reused or removed "
                    f"({reason}). Pass --control-path to use another path, or "
                    f"close it: ssh -O exit -S {shlex.quote(create_path)} _"
                )
        _remove_stale_socket_file(create_path)

    socket_parent = Path(create_path).parent
    if not socket_parent.exists():
        socket_parent.mkdir(parents=True, exist_ok=True)
        os.chmod(socket_parent, 0o700)

    argv = [
        "ssh",
        "-M",
        "-S", create_path,
        "-f",
        "-N",
        "-o", "ControlMaster=yes",
        "-o", "ControlPersist=yes",
        "-o", f"ServerAliveInterval={interval}",
        "-o", f"ServerAliveCountMax={count_max}",
    ]
    if effective_port is not None:
        argv += ["-p", str(effective_port)]
    argv.append(ssh_target)

    # Redirect ssh stdout -> stderr so remote banners can't pollute our stdout
    # (which prints only the opened ControlMaster path). Interactive auth
    # prompts use /dev/tty and stderr directly.
    try:
        proc = subprocess.run(argv, stdout=sys.stderr)
    except FileNotFoundError as e:
        raise RuntimeError("ssh binary not found. Ensure OpenSSH is installed.") from e

    if proc.returncode != 0:
        raise RuntimeError(
            f"failed to open ControlMaster for {display_name} "
            f"(ssh exit {proc.returncode})."
        )

    alive, pid, msg = check_master(create_path, ssh_target, effective_port)
    if not alive:
        raise RuntimeError(
            f"ControlMaster did not become active at {create_path}: {msg}"
        )

    registry[create_path] = {
        "control_path": create_path,
        "target": ssh_target,
        "host_arg": display_name,
        "port": ssh_resolved.port,
        "pid": pid,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "server_alive_interval": interval,
        "server_alive_count_max": count_max,
    }
    _save_registry(registry_path, registry)

    return PersistResult(
        control_path=create_path,
        target=ssh_target,
        display_name=display_name,
        reused=False,
        pid=pid,
        interval=interval,
        count_max=count_max,
    )


def _discover_onirika_masters(
    *,
    config_path: Path | None,
    registry: dict[str, dict],
    ssh_dir: Path,
) -> dict[str, dict]:
    """Collect all Onirika-managed ControlMaster entries across sources.

    Sources:
      1. Local registry (``masters.json``) — created by ``onirika persist``.
      2. Dedicated Onirika socket files in ``~/.ssh/onirika-ctrl-*``.
      3. Configured host control paths in ``config.yaml`` (only if present on disk).
    """
    discovered: dict[str, dict] = {}

    # 1. Registry entries (even if socket file is already gone, so we can prune).
    for path, meta in registry.items():
        norm = _normalize_path(path)
        discovered[norm] = {
            "control_path": norm,
            "target": meta.get("target"),
            "port": meta.get("port"),
            "in_registry": True,
        }

    # 2. Dedicated Onirika socket files in ~/.ssh/.
    if ssh_dir.is_dir():
        try:
            for entry in ssh_dir.iterdir():
                if not entry.name.startswith(ONIRIKA_SOCKET_PREFIX):
                    continue
                norm = _normalize_path(str(entry))
                if norm not in discovered and _is_unix_socket(norm):
                    discovered[norm] = {
                        "control_path": norm,
                        "target": None,
                        "port": None,
                        "in_registry": False,
                    }
        except OSError:
            pass

    # 3. Configured Onirika hosts from config.yaml (only existing sockets).
    try:
        cfg = load_config(config_path)
    except FileNotFoundError:
        if config_path is not None:
            raise
        cfg = None
    except ValueError:
        if config_path is not None:
            raise
        cfg = None

    if cfg is not None:
        for host_cfg in cfg.hosts.values():
            target = host_cfg.ssh_target
            port = host_cfg.port if host_cfg.port != 22 else None
            exec_resolved = resolve_ssh_config(
                target,
                port=port,
                control_path_override=host_cfg.resolved_control_path,
            )
            raw_resolved = resolve_ssh_config(
                target,
                port=port,
                control_path_override=os.path.expanduser(host_cfg.control_path),
            )
            for candidate in _dedupe([
                exec_resolved.control_path,
                host_cfg.resolved_control_path,
                raw_resolved.control_path,
            ]):
                if candidate not in discovered and _is_unix_socket(candidate):
                    discovered[candidate] = {
                        "control_path": candidate,
                        "target": target,
                        "port": host_cfg.port,
                        "in_registry": False,
                    }

    return discovered


def cleanup_masters(
    *,
    force: bool = False,
    config_path: Path | None = None,
    registry_path: Path | None = None,
    ssh_dir: Path | None = None,
) -> list[CleanupItem]:
    """Remove stale sockets. With ``force=True``, close ``onirika-ctrl-*`` masters.

    Returns a list of ``CleanupItem`` describing each discovered ControlMaster
    and the action taken.
    """
    registry_path = registry_path or default_registry_path()
    ssh_dir = ssh_dir or default_ssh_dir()

    registry = _load_registry(registry_path)
    discovered = _discover_onirika_masters(
        config_path=config_path,
        registry=registry,
        ssh_dir=ssh_dir,
    )

    items: list[CleanupItem] = []
    registry_changed = False

    for path in sorted(discovered.keys()):
        meta = discovered[path]
        target = meta.get("target")
        port = meta.get("port")

        if not os.path.lexists(path):
            if path in registry:
                del registry[path]
                registry_changed = True
            items.append(
                CleanupItem(
                    control_path=path,
                    target=target,
                    status="pruned_missing",
                    detail="socket no longer exists",
                )
            )
            continue

        if not _is_unix_socket(path):
            if path in registry:
                del registry[path]
                registry_changed = True
            items.append(
                CleanupItem(
                    control_path=path,
                    target=target,
                    status="failed",
                    detail="path is not a Unix socket; left untouched",
                )
            )
            continue

        state, error = probe_socket(path)

        if state == "unknown":
            items.append(
                CleanupItem(
                    control_path=path,
                    target=target,
                    status="failed",
                    detail=f"cannot probe socket; left untouched: {error}",
                )
            )
            continue

        if state != "live":
            try:
                _remove_stale_socket_file(path)
                if path in registry:
                    del registry[path]
                    registry_changed = True
                items.append(
                    CleanupItem(
                        control_path=path,
                        target=target,
                        status="removed_stale",
                        detail=error,
                    )
                )
            except OSError as e:
                items.append(
                    CleanupItem(
                        control_path=path,
                        target=target,
                        status="failed",
                        detail=str(e),
                    )
                )
            continue

        # Master is live. `ssh -O check` is used only for the PID.
        _, pid, check_msg = check_master(path, target, port)

        if not force or not Path(path).name.startswith(ONIRIKA_SOCKET_PREFIX):
            items.append(
                CleanupItem(
                    control_path=path,
                    target=target,
                    status="kept_active",
                    pid=pid,
                    detail=check_msg,
                )
            )
            continue

        _, exit_msg = exit_master(path, target, port)
        state = _probe_after_exit(path)
        if state == "stale":
            try:
                _remove_stale_socket_file(path)
            except OSError:
                pass

        if state in ("stale", "missing"):
            if path in registry:
                del registry[path]
                registry_changed = True
            items.append(
                CleanupItem(
                    control_path=path,
                    target=target,
                    status="closed_active",
                    pid=pid,
                    detail=exit_msg,
                )
            )
        else:
            items.append(
                CleanupItem(
                    control_path=path,
                    target=target,
                    status="failed",
                    pid=pid,
                    detail=exit_msg or "failed to close active ControlMaster",
                )
            )

    if registry_changed:
        _save_registry(registry_path, registry)

    return items
