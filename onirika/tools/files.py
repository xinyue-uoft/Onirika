"""File operation tools."""

from __future__ import annotations

import os
import shlex

from onirika.server import mcp, require_connection


@mcp.tool()
async def ssh_read_file(
    path: str,
    offset: int = 0,
    limit: int = 2000,
    host: str | None = None,
) -> dict:
    """Read lines from a file on the remote server.

    Efficiently reads only the requested range using sed, so large files
    are safe to read partially. Returns line-numbered content similar to
    `cat -n` output.

    Args:
        path: Absolute or relative path to the file on the remote.
        offset: Line number to start reading from (0-based).
        limit: Maximum number of lines to return.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    offset = int(offset)
    limit = int(limit)
    safe_path = shlex.quote(path)

    # Check if file is binary
    check = await executor.run(f"file --mime-encoding {safe_path}", cwd="~")
    if check.exit_code != 0:
        return {"error": "file_not_found", "message": check.stderr.strip()}
    if "binary" in check.stdout.lower():
        # Get file info instead
        stat = await executor.run(
            f"stat --format='%s bytes, %A, modified %y' {safe_path} 2>/dev/null || "
            f"stat -f '%z bytes, %Sp, modified %Sm' {safe_path}",
            cwd="~",
        )
        return {
            "error": "binary_file",
            "message": f"File appears to be binary ({stat.stdout.strip()}). Use ssh_file_info to inspect, or ssh_run with 'xxd' / 'hexdump' for binary content.",
        }

    # Get total line count and the requested range
    start = offset + 1  # sed is 1-based
    end = offset + limit
    result = await executor.run(
        f"wc -l < {safe_path} && sed -n '{start},{end}p' {safe_path}",
        cwd="~",
        timeout=executor.config.file_timeout,
    )

    if result.exit_code != 0:
        return {"error": "read_failed", "message": result.stderr.strip()}

    parts = result.stdout.split("\n", 1)
    try:
        total_lines = int(parts[0].strip())
    except ValueError:
        total_lines = -1
    content = parts[1] if len(parts) > 1 else ""

    # Add line numbers
    numbered_lines = []
    for i, line in enumerate(content.rstrip("\n").split("\n")):
        numbered_lines.append(f"{offset + i + 1}\t{line}")
    numbered_content = "\n".join(numbered_lines)

    return {
        "content": numbered_content,
        "total_lines": total_lines,
        "showing": f"lines {offset + 1}-{min(end, total_lines)}",
        "truncated": total_lines > end,
    }


@mcp.tool()
async def ssh_write_file(
    path: str,
    content: str,
    create_dirs: bool = False,
    host: str | None = None,
) -> dict:
    """Write content to a file on the remote server.

    Uses atomic write (temp file + mv) to prevent partial writes.
    Overwrites the file if it exists, creates it if it doesn't.

    Args:
        path: Absolute or relative path on the remote.
        content: Text content to write.
        create_dirs: If true, create parent directories if they don't exist.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)

    max_size = executor.config.max_file_size
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > max_size:
        return {
            "error": "file_too_large",
            "message": f"Content is {len(content_bytes)} bytes, exceeds max {max_size} bytes.",
        }

    if create_dirs:
        safe_dir = shlex.quote(path.rsplit("/", 1)[0]) if "/" in path else "."
        await executor.run(f"mkdir -p {safe_dir}", cwd="~")

    result = await executor.write_file_raw(path, content_bytes)

    if result.exit_code != 0:
        return {"error": "write_failed", "message": result.stderr.strip()}

    return {
        "success": True,
        "bytes_written": len(content_bytes),
        "path": path,
    }


@mcp.tool()
async def ssh_list_dir(
    path: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    max_entries: int = 500,
    host: str | None = None,
) -> dict:
    """List directory contents on the remote server.

    Args:
        path: Directory path to list. Defaults to current working directory.
        pattern: Glob pattern to filter entries (e.g., "*.py", "Makefile").
        recursive: If true, list recursively.
        max_entries: Maximum entries to return (prevents overwhelming output).
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    safe_path = shlex.quote(path)
    safe_pattern = shlex.quote(pattern)
    max_entries = int(max_entries)

    if recursive:
        # Use -exec ls instead of -printf for BSD/macOS compatibility
        cmd = (
            f"find {safe_path} -name {safe_pattern} "
            f"-maxdepth 10 -exec ls -ld {{}} + 2>/dev/null | "
            f"head -n {max_entries + 1}"
        )
    else:
        # Use find instead of ls glob to avoid shell expansion of pattern
        if pattern == "*":
            cmd = f"ls -la {safe_path} 2>/dev/null | head -n {max_entries + 2}"
        else:
            cmd = f"find {safe_path} -maxdepth 1 -name {safe_pattern} -exec ls -ld {{}} + 2>/dev/null | head -n {max_entries + 1}"

    result = await executor.run(cmd, cwd="~")

    if result.exit_code != 0 and not result.stdout.strip():
        return {"error": "list_failed", "message": result.stderr.strip() or "Directory not found or empty"}

    lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

    if recursive:
        # ls -ld output: just pass through the lines
        entries = []
        for line in lines[:max_entries]:
            if line.startswith("total "):
                continue
            entries.append(line)
        truncated = len(lines) > max_entries
    else:
        # ls -la output: skip the "total N" line
        entries = []
        for line in lines:
            if line.startswith("total "):
                continue
            entries.append(line)
        truncated = len(entries) > max_entries
        entries = entries[:max_entries]

    return {
        "entries": entries,
        "count": len(entries),
        "truncated": truncated,
        "path": path,
    }


@mcp.tool()
async def ssh_file_info(
    path: str,
    host: str | None = None,
) -> dict:
    """Get detailed information about a file or directory on the remote server.

    Args:
        path: Path to inspect.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    safe_path = shlex.quote(path)

    # Try GNU stat first, fall back to BSD stat
    result = await executor.run(
        f"stat --format='%F|%s|%U|%G|%A|%y' {safe_path} 2>/dev/null || "
        f"stat -f '%HT|%z|%Su|%Sg|%Sp|%Sm' {safe_path}",
        cwd="~",
    )

    if result.exit_code != 0:
        return {"error": "not_found", "message": result.stderr.strip()}

    parts = result.stdout.strip().split("|")
    if len(parts) >= 6:
        return {
            "exists": True,
            "type": parts[0],
            "size": int(parts[1]) if parts[1].isdigit() else parts[1],
            "owner": parts[2],
            "group": parts[3],
            "permissions": parts[4],
            "modified": parts[5],
            "path": path,
        }

    return {
        "exists": True,
        "raw": result.stdout.strip(),
        "path": path,
    }


@mcp.tool()
async def ssh_patch_file(
    path: str,
    old: str,
    new: str,
    host: str | None = None,
) -> dict:
    """Replace a string in a file on the remote server.

    Reads the file, performs the replacement locally, and writes it back.
    The old string must appear exactly once (to avoid ambiguous edits).

    Args:
        path: Path to the file on the remote.
        old: The exact text to find and replace. Must be unique in the file.
        new: The replacement text.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)

    # Read the current file
    result = await executor.read_file_raw(path)
    if result.exit_code != 0:
        return {"error": "read_failed", "message": result.stderr.strip()}

    content = result.stdout
    count = content.count(old)
    if count == 0:
        return {"error": "not_found", "message": "The old string was not found in the file."}
    if count > 1:
        return {
            "error": "ambiguous",
            "message": f"The old string appears {count} times. Provide a larger unique context.",
        }

    new_content = content.replace(old, new, 1)
    write_result = await executor.write_file_raw(path, new_content.encode("utf-8"))

    if write_result.exit_code != 0:
        return {"error": "write_failed", "message": write_result.stderr.strip()}

    return {"success": True, "path": path}


@mcp.tool()
async def ssh_mkdir(
    path: str,
    parents: bool = True,
    host: str | None = None,
) -> dict:
    """Create a directory on the remote server.

    Args:
        path: Directory path to create.
        parents: If true, create parent directories as needed (mkdir -p).
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    safe_path = shlex.quote(path)
    flag = "-p " if parents else ""
    result = await executor.run(f"mkdir {flag}{safe_path}", cwd="~")

    if result.exit_code != 0:
        return {"error": "mkdir_failed", "message": result.stderr.strip()}
    return {"success": True, "path": path}


@mcp.tool()
async def ssh_move(
    source: str,
    dest: str,
    host: str | None = None,
) -> dict:
    """Move or rename a file/directory on the remote server.

    Args:
        source: Current path.
        dest: New path.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    result = await executor.run(
        f"mv {shlex.quote(source)} {shlex.quote(dest)}", cwd="~"
    )

    if result.exit_code != 0:
        return {"error": "move_failed", "message": result.stderr.strip()}
    return {"success": True, "source": source, "dest": dest}


@mcp.tool()
async def ssh_delete(
    path: str,
    recursive: bool = False,
    host: str | None = None,
) -> dict:
    """Delete a file or directory on the remote server.

    Args:
        path: Path to delete.
        recursive: If true, delete directories recursively (rm -r).
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    flag = "-r " if recursive else ""
    result = await executor.run(f"rm {flag}{shlex.quote(path)}", cwd="~")

    if result.exit_code != 0:
        return {"error": "delete_failed", "message": result.stderr.strip()}
    return {"success": True, "path": path}


@mcp.tool()
async def ssh_download(
    remote_path: str,
    local_dir: str = "",
    host: str | None = None,
) -> dict:
    """Download a text file from the remote server to the local machine.

    Note: This transfers content as UTF-8 text. Binary files may be corrupted.
    For binary files, use ssh_run with scp or base64 encoding instead.

    Args:
        remote_path: Path to the file on the remote server.
        local_dir: Local directory to save to. Defaults to ~/Downloads.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    safe_remote = shlex.quote(remote_path)

    if not local_dir:
        local_dir = os.path.expanduser("~/Downloads")

    # Resolve to absolute path and ensure it's under home directory
    local_dir = os.path.realpath(os.path.expanduser(local_dir))
    home = os.path.realpath(os.path.expanduser("~"))
    if not local_dir.startswith(home):
        return {"error": "invalid_path", "message": f"local_dir must be under your home directory ({home})."}

    os.makedirs(local_dir, exist_ok=True)
    filename = os.path.basename(remote_path)
    if not filename or filename.startswith("."):
        return {"error": "invalid_filename", "message": "Remote path must have a non-hidden filename."}
    local_path = os.path.join(local_dir, filename)

    # Read via SSH and write locally
    result = await executor.run(f"cat {safe_remote}", cwd="~", timeout=executor.config.file_timeout)

    if result.exit_code != 0:
        return {"error": "download_failed", "message": result.stderr.strip()}

    # Write raw bytes to local file
    content_bytes = result.stdout.encode("utf-8")
    with open(local_path, "wb") as f:
        f.write(content_bytes)

    return {
        "success": True,
        "local_path": local_path,
        "size": len(content_bytes),
    }


@mcp.tool()
async def ssh_append_file(
    path: str,
    content: str,
    host: str | None = None,
) -> dict:
    """Append text to the end of a file on the remote server.

    Efficient for adding content to large files — no read round-trip needed.
    The file is created if it does not exist.

    Args:
        path: Path to the file on the remote.
        content: Text content to append.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    safe_path = shlex.quote(path)
    content_bytes = content.encode("utf-8")

    result = await executor.run(
        f"cat >> {safe_path}",
        cwd="~",
        timeout=executor.config.file_timeout,
        input_data=content_bytes,
    )

    if result.exit_code != 0:
        return {"error": "append_failed", "message": result.stderr.strip()}

    return {"success": True, "bytes_appended": len(content_bytes), "path": path}


@mcp.tool()
async def ssh_insert_lines(
    path: str,
    after_line: int,
    content: str,
    host: str | None = None,
) -> dict:
    """Insert text after a specific line number in a file on the remote server.

    Operates directly via sed on the remote — no full-file transfer needed.
    Only the new content is sent over the wire.

    Args:
        path: Path to the file on the remote.
        after_line: Line number to insert after (1-based). Use 0 to insert at the beginning.
        content: Text content to insert.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    after_line = int(after_line)
    safe_path = shlex.quote(path)
    content_bytes = content.encode("utf-8")

    if after_line == 0:
        # Insert at the beginning: write content to temp, then prepend
        script = (
            f"_tmp=$(mktemp) && cat > \"$_tmp\" && "
            f"_orig=$(mktemp) && cp {safe_path} \"$_orig\" && "
            f"cat \"$_tmp\" \"$_orig\" > {safe_path} && "
            f"rm -f \"$_tmp\" \"$_orig\""
        )
    else:
        # Insert after a specific line: write content to temp, use sed 'r' + mv (portable)
        script = (
            f"_tmp=$(mktemp) && _out=$(mktemp) && cat > \"$_tmp\" && "
            f"sed '{after_line}r '\"$_tmp\" {safe_path} > \"$_out\" && "
            f"mv \"$_out\" {safe_path} && "
            f"rm -f \"$_tmp\""
        )

    result = await executor.run(
        script, cwd="~", timeout=executor.config.file_timeout, input_data=content_bytes,
    )

    if result.exit_code != 0:
        return {"error": "insert_failed", "message": result.stderr.strip()}

    return {"success": True, "after_line": after_line, "path": path}


@mcp.tool()
async def ssh_replace_lines(
    path: str,
    start_line: int,
    end_line: int,
    content: str = "",
    host: str | None = None,
) -> dict:
    """Replace a range of lines in a file on the remote server.

    Operates directly via sed on the remote — no full-file transfer needed.
    If content is empty, the lines are deleted. Both start_line and end_line
    are 1-based and inclusive.

    Args:
        path: Path to the file on the remote.
        start_line: First line to replace (1-based, inclusive).
        end_line: Last line to replace (1-based, inclusive).
        content: Replacement text. Empty string deletes the line range.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    start_line = int(start_line)
    end_line = int(end_line)
    safe_path = shlex.quote(path)

    if start_line < 1 or end_line < start_line:
        return {
            "error": "invalid_range",
            "message": f"Invalid line range: {start_line}-{end_line}. start_line must be >= 1 and <= end_line.",
        }

    if not content:
        # Just delete the range — use sed > tmp + mv for portability (no sed -i)
        script = (
            f"_out=$(mktemp) && "
            f"sed '{start_line},{end_line}d' {safe_path} > \"$_out\" && "
            f"mv \"$_out\" {safe_path}"
        )
        result = await executor.run(script, cwd="~", timeout=executor.config.file_timeout)
    else:
        # Delete range, then insert replacement after (start_line - 1)
        content_bytes = content.encode("utf-8")
        insert_after = start_line - 1
        if insert_after == 0:
            # Replacing from line 1: delete range, prepend content
            script = (
                f"_tmp=$(mktemp) && _out=$(mktemp) && cat > \"$_tmp\" && "
                f"sed '{start_line},{end_line}d' {safe_path} > \"$_out\" && "
                f"cat \"$_tmp\" \"$_out\" > {safe_path} && "
                f"rm -f \"$_tmp\" \"$_out\""
            )
        else:
            script = (
                f"_tmp=$(mktemp) && _out=$(mktemp) && _out2=$(mktemp) && cat > \"$_tmp\" && "
                f"sed '{start_line},{end_line}d' {safe_path} > \"$_out\" && "
                f"sed '{insert_after}r '\"$_tmp\" \"$_out\" > \"$_out2\" && "
                f"mv \"$_out2\" {safe_path} && "
                f"rm -f \"$_tmp\" \"$_out\""
            )
        result = await executor.run(
            script, cwd="~", timeout=executor.config.file_timeout, input_data=content_bytes,
        )

    if result.exit_code != 0:
        return {"error": "replace_failed", "message": result.stderr.strip()}

    return {
        "success": True,
        "replaced": f"lines {start_line}-{end_line}",
        "path": path,
    }
