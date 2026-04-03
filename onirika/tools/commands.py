"""Command execution tools."""

from __future__ import annotations

import re
import shlex
import uuid

from onirika.server import mcp, require_connection

_SAFE_JOB_ID = re.compile(r"^[a-zA-Z0-9_-]+$")


@mcp.tool()
async def ssh_run(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
    host: str | None = None,
) -> dict:
    """Execute a shell command on the remote server.

    The command runs in a bash login shell with the configured environment
    preamble (e.g., sourced setup scripts) already applied.

    Args:
        command: Shell command to run (e.g., "make test", "python script.py").
        cwd: Working directory on the remote. Defaults to the host's configured default_cwd.
        timeout: Timeout in seconds. Defaults to host config (usually 30s).
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    result = await executor.run(command, cwd=cwd, timeout=timeout)

    # Truncate very long output
    max_lines = executor.config.max_output_lines
    stdout_lines = result.stdout.split("\n")
    truncated = len(stdout_lines) > max_lines
    if truncated:
        stdout = "\n".join(stdout_lines[:max_lines]) + f"\n\n[truncated — {len(stdout_lines)} total lines, showing first {max_lines}]"
    else:
        stdout = result.stdout

    return {
        "stdout": stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "truncated": truncated,
    }


_JOBS_DIR = "/tmp/.onirika_jobs"


@mcp.tool()
async def ssh_run_background(
    command: str,
    cwd: str | None = None,
    job_id: str = "",
    host: str | None = None,
) -> dict:
    """Start a long-running command in the background on the remote server.

    Use this for commands that take more than 30 seconds (e.g., builds, test suites).
    The command runs detached via nohup. Use ssh_job_status to check progress and
    retrieve output.

    Args:
        command: Shell command to run in the background.
        cwd: Working directory on the remote. Defaults to host's default_cwd.
        job_id: Optional human-readable job ID. Auto-generated if omitted.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)

    if not job_id:
        job_id = uuid.uuid4().hex[:8]

    if not _SAFE_JOB_ID.match(job_id):
        return {"error": "invalid_job_id", "message": "job_id must contain only alphanumeric characters, hyphens, and underscores."}

    job_dir = shlex.quote(f"{_JOBS_DIR}/{job_id}")
    safe_cmd = shlex.quote(command)

    # Create job directory, write command, launch in background, capture PID and exit code
    launch_script = (
        f"mkdir -p {job_dir} && "
        f"echo {safe_cmd} > {job_dir}/command && "
        f"nohup bash -c {safe_cmd} "
        f"> {job_dir}/stdout.log 2> {job_dir}/stderr.log & "
        f"BGPID=$! && "
        f"echo $BGPID > {job_dir}/pid && "
        # Reaper: waits for process to finish, writes exit code
        f"(wait $BGPID 2>/dev/null; echo $? > {job_dir}/exit_code) & "
        f"echo $BGPID"
    )

    result = await executor.run(launch_script, cwd=cwd, timeout=10)

    if result.exit_code != 0:
        return {"error": "launch_failed", "message": result.stderr.strip()}

    pid = result.stdout.strip().split("\n")[-1]
    return {
        "job_id": job_id,
        "pid": pid,
        "job_dir": job_dir,
        "message": f"Job '{job_id}' started (PID {pid}). Use ssh_job_status(job_id='{job_id}') to check progress.",
    }


@mcp.tool()
async def ssh_job_status(
    job_id: str,
    tail_lines: int = 50,
    host: str | None = None,
) -> dict:
    """Check the status of a background job and retrieve its output.

    Args:
        job_id: The job ID returned by ssh_run_background.
        tail_lines: Number of lines to show from the end of stdout/stderr.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    tail_lines = int(tail_lines)

    if not _SAFE_JOB_ID.match(job_id):
        return {"error": "invalid_job_id", "message": "job_id must contain only alphanumeric characters, hyphens, and underscores."}

    job_dir = shlex.quote(f"{_JOBS_DIR}/{job_id}")

    # Check if job directory exists
    check = await executor.run(f"test -d {job_dir} && echo ok", cwd="~")
    if "ok" not in check.stdout:
        return {"error": "job_not_found", "message": f"No job with ID '{job_id}' found."}

    # Check if still running via PID, and get exit code if done
    status_script = (
        f"PID=$(cat {job_dir}/pid 2>/dev/null) && "
        f"if kill -0 $PID 2>/dev/null; then "
        f"  echo 'status:running'; "
        f"else "
        f"  EC=$(cat {job_dir}/exit_code 2>/dev/null || echo unknown); "
        f"  echo \"status:exited:$EC\"; "
        f"fi"
    )
    status_result = await executor.run(status_script, cwd="~")
    status_line = status_result.stdout.strip()

    running = "status:running" in status_line
    exit_code = None
    if not running and "exited:" in status_line:
        ec_str = status_line.split("exited:")[-1]
        try:
            exit_code = int(ec_str)
        except ValueError:
            pass

    # Get tail of stdout and stderr
    stdout_result = await executor.run(
        f"tail -n {tail_lines} {job_dir}/stdout.log 2>/dev/null", cwd="~"
    )
    stderr_result = await executor.run(
        f"tail -n {tail_lines} {job_dir}/stderr.log 2>/dev/null", cwd="~"
    )

    return {
        "job_id": job_id,
        "running": running,
        "exit_code": exit_code,
        "stdout_tail": stdout_result.stdout,
        "stderr_tail": stderr_result.stdout,
    }
