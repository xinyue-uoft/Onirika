"""Search tools — grep and find on the remote server."""

from __future__ import annotations

import shlex

from onirika.server import mcp, require_connection


@mcp.tool()
async def ssh_grep(
    pattern: str,
    path: str = ".",
    glob: str = "",
    max_results: int = 100,
    context_lines: int = 0,
    case_insensitive: bool = False,
    host: str | None = None,
) -> dict:
    """Search for a pattern in files on the remote server using grep.

    Returns matching lines with file paths and line numbers.

    Args:
        pattern: Regular expression pattern to search for.
        path: Directory or file to search in.
        glob: Filter files by glob pattern (e.g., "*.py", "*.cpp").
        max_results: Maximum number of matching lines to return.
        context_lines: Number of context lines to show before and after each match.
        case_insensitive: If true, ignore case when matching.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    safe_path = shlex.quote(path)
    safe_pattern = shlex.quote(pattern)
    context_lines = int(context_lines)
    max_results = int(max_results)

    args = ["grep", "-rn"]
    if case_insensitive:
        args.append("-i")
    if context_lines > 0:
        args.append(f"-C{context_lines}")
    if glob:
        args.extend(["--include", shlex.quote(glob)])
    args.append(safe_pattern)
    args.append(safe_path)

    cmd = " ".join(args) + f" | head -n {max_results + 1}"
    result = await executor.run(cmd)

    if result.exit_code == 1 and not result.stdout.strip():
        return {"matches": [], "count": 0, "truncated": False, "message": "No matches found."}

    if result.exit_code > 1:
        return {"error": "grep_failed", "message": result.stderr.strip()}

    lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
    truncated = len(lines) > max_results
    lines = lines[:max_results]

    return {
        "matches": lines,
        "count": len(lines),
        "truncated": truncated,
    }


@mcp.tool()
async def ssh_find_files(
    path: str = ".",
    pattern: str = "*",
    file_type: str = "f",
    max_results: int = 200,
    max_depth: int = 10,
    host: str | None = None,
) -> dict:
    """Find files by name pattern on the remote server.

    Args:
        path: Directory to search in.
        pattern: Filename glob pattern (e.g., "*.py", "test_*").
        file_type: Type filter: "f" for files, "d" for directories, "" for both.
        max_results: Maximum entries to return.
        max_depth: Maximum directory depth to search.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    safe_path = shlex.quote(path)
    max_depth = int(max_depth)
    max_results = int(max_results)

    # Validate file_type against allowlist to prevent injection
    valid_types = {"f", "d", "l", "b", "c", "p", "s", ""}
    if file_type not in valid_types:
        return {"error": "invalid_type", "message": f"file_type must be one of: {', '.join(sorted(valid_types - {''}))} (or empty)"}

    args = ["find", safe_path, "-maxdepth", str(max_depth)]
    if file_type:
        args.extend(["-type", file_type])
    args.extend(["-name", shlex.quote(pattern)])
    cmd = " ".join(args) + f" 2>/dev/null | head -n {max_results + 1}"

    result = await executor.run(cmd)

    lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
    truncated = len(lines) > max_results
    lines = lines[:max_results]

    return {
        "files": lines,
        "count": len(lines),
        "truncated": truncated,
    }
