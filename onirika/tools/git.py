"""Git operation tools on the remote server."""

from __future__ import annotations

import shlex

from onirika.server import mcp, require_connection


@mcp.tool()
async def ssh_git_status(
    cwd: str | None = None,
    host: str | None = None,
) -> dict:
    """Get git status of a repository on the remote server.

    Returns the current branch, modified files, staged files, and untracked files.

    Args:
        cwd: Path to the git repository. Defaults to host's default_cwd.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)

    result = await executor.run("git status --porcelain -b", cwd=cwd)
    if result.exit_code != 0:
        return {"error": "git_failed", "message": result.stderr.strip()}

    lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
    branch = ""
    modified = []
    staged = []
    untracked = []

    for line in lines:
        if line.startswith("## "):
            branch = line[3:].split("...")[0]
            continue
        if len(line) < 2:
            continue
        index_status = line[0]
        work_status = line[1]
        filepath = line[3:]

        if index_status == "?":
            untracked.append(filepath)
        else:
            if index_status in ("M", "A", "D", "R"):
                staged.append(f"{index_status} {filepath}")
            if work_status in ("M", "D"):
                modified.append(f"{work_status} {filepath}")

    return {
        "branch": branch,
        "staged": staged,
        "modified": modified,
        "untracked": untracked,
        "clean": not (staged or modified or untracked),
    }


@mcp.tool()
async def ssh_git_diff(
    cwd: str | None = None,
    ref: str = "",
    path: str = "",
    staged: bool = False,
    host: str | None = None,
) -> dict:
    """Show git diff on the remote server.

    Args:
        cwd: Path to the git repository. Defaults to host's default_cwd.
        ref: Git ref to diff against (e.g., "HEAD~1", "main", a commit hash). Empty for working tree diff.
        path: Limit diff to a specific file or directory.
        staged: If true, show staged changes (--cached).
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)

    cmd_parts = ["git", "diff"]
    if staged:
        cmd_parts.append("--cached")
    if ref:
        cmd_parts.append(shlex.quote(ref))
    if path:
        cmd_parts.extend(["--", shlex.quote(path)])

    result = await executor.run(" ".join(cmd_parts), cwd=cwd)

    if result.exit_code != 0:
        return {"error": "git_failed", "message": result.stderr.strip()}

    diff = result.stdout
    lines = diff.split("\n")
    max_lines = executor.config.max_output_lines
    truncated = len(lines) > max_lines
    if truncated:
        diff = "\n".join(lines[:max_lines]) + f"\n\n[truncated — {len(lines)} total lines]"

    return {
        "diff": diff,
        "truncated": truncated,
        "empty": not diff.strip(),
    }


@mcp.tool()
async def ssh_git_log(
    cwd: str | None = None,
    count: int = 10,
    path: str = "",
    oneline: bool = False,
    host: str | None = None,
) -> dict:
    """Show git log on the remote server.

    Args:
        cwd: Path to the git repository. Defaults to host's default_cwd.
        count: Number of commits to show.
        path: Limit log to a specific file or directory.
        oneline: If true, show compact one-line format.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    count = int(count)

    if oneline:
        fmt = "--oneline"
    else:
        fmt = "--format=%H|%an|%ai|%s"

    cmd_parts = ["git", "log", fmt, f"-n{count}"]
    if path:
        cmd_parts.extend(["--", shlex.quote(path)])

    result = await executor.run(" ".join(cmd_parts), cwd=cwd)

    if result.exit_code != 0:
        return {"error": "git_failed", "message": result.stderr.strip()}

    if oneline:
        return {"log": result.stdout.strip(), "count": count}

    commits = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) >= 4:
            commits.append({
                "hash": parts[0],
                "author": parts[1],
                "date": parts[2],
                "message": parts[3],
            })

    return {"commits": commits, "count": len(commits)}
