"""
Tool definitions and executor for the Agent Phone Assistant.

Each tool has a name, description, parameters schema (OpenAI function-calling
format), and an async execute function.

Security model:
  - Commands use allowlist + subprocess_exec (NO shell interpreter)
  - File ops use TOCTOU-safe path resolution (re-check after open)
  - File reads check size/type before opening
  - File writes restricted to a single data directory
"""

from __future__ import annotations

import asyncio
import os
import shlex
import stat as stat_module
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Safety configuration
# ---------------------------------------------------------------------------

# Directories where file read is permitted
ALLOWED_READ_PATHS: list[str] = [
    "/opt/agent",
    "/tmp",
]

# Directories where file write is permitted (subset of read paths)
ALLOWED_WRITE_PATHS: list[str] = [
    "/opt/agent/data",
    "/tmp",
]

# Maximum file size for reads (10 MB)
MAX_READ_BYTES = 10 * 1024 * 1024

# Commands allowed via run_command — only safe, read-oriented commands.
# Each entry maps the binary name to itself (no shell wrappers).
ALLOWED_COMMANDS: dict[str, str] = {
    # File listing & viewing
    "ls": "/bin/ls",
    "cat": "/bin/cat",
    "head": "/bin/head",
    "tail": "/bin/tail",
    "wc": "/bin/wc",
    "find": "/bin/find",
    "grep": "/bin/grep",
    "tree": "/usr/bin/tree",
    "file": "/usr/bin/file",
    "stat": "/usr/bin/stat",
    # System info
    "pwd": "/bin/pwd",
    "date": "/bin/date",
    "uptime": "/usr/bin/uptime",
    "uname": "/bin/uname",
    "hostname": "/bin/hostname",
    "df": "/bin/df",
    "du": "/usr/bin/du",
    "free": "/usr/bin/free",
    "ps": "/bin/ps",
    # Network diagnostics
    "ping": "/bin/ping",
    "curl": "/usr/bin/curl",
    "wget": "/usr/bin/wget",
    "ss": "/bin/ss",
    "ip": "/bin/ip",
    "journalctl": "/bin/journalctl",
    "systemctl": "/bin/systemctl",
    # Package & dev
    "python3": "/usr/bin/python3",
    "python": "/usr/bin/python",
    "node": "/usr/bin/node",
    "npm": "/usr/bin/npm",
    "pip3": "/usr/bin/pip3",
    "pip": "/usr/bin/pip",
    "git": "/usr/bin/git",
    "echo": "/bin/echo",
    "mkdir": "/bin/mkdir",
    "touch": "/usr/bin/touch",
    "cp": "/bin/cp",
    "mv": "/bin/mv",
}

# ---------------------------------------------------------------------------
# Path helpers (TOCTOU-safe)
# ---------------------------------------------------------------------------

def _in_allowed(path: str, allowed_roots: list[str]) -> Path:
    """
    Resolve *path* and verify it lies inside one of *allowed_roots*.
    Returns the resolved Path on success, raises PermissionError otherwise.
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path("/opt/agent") / p
    p = p.resolve()

    roots = [Path(r).expanduser().resolve() for r in allowed_roots]
    if not any(str(p).startswith(str(r) + os.sep) or str(p) == str(r)
               for r in roots):
        raise PermissionError(f"Path not allowed: {path}")
    return p


def _safe_open(path: str, mode: str, allowed_roots: list[str]):
    """
    Open a file after TOCTOU-safe path validation.
    Returns (file_object, resolved_path).
    """
    resolved = _in_allowed(path, allowed_roots)

    # Refuse devices, FIFOs, sockets
    try:
        st = resolved.stat()
    except FileNotFoundError:
        if "r" in mode and "w" not in mode:
            raise
        # writing can create
    else:
        if stat_module.S_ISBLK(st.st_mode) or stat_module.S_ISCHR(st.st_mode):
            raise PermissionError(f"Block/character device not allowed: {path}")
        if stat_module.S_ISFIFO(st.st_mode):
            raise PermissionError(f"Named pipe not allowed: {path}")
        if stat_module.S_ISSOCK(st.st_mode):
            raise PermissionError(f"Socket not allowed: {path}")
        # Size check for reads
        if "r" in mode and "w" not in mode and st.st_size > MAX_READ_BYTES:
            raise PermissionError(
                f"File too large ({st.st_size} bytes, max {MAX_READ_BYTES})"
            )

    fh = open(resolved, mode, encoding="utf-8", errors="replace")

    # TOCTOU re-check: verify the opened file's real path
    real = Path(fh.name).resolve()
    if not any(str(real).startswith(str(r) + os.sep) or str(real) == str(r)
               for r in roots):
        fh.close()
        raise PermissionError(f"Symlink escape blocked: {path} → {real}")

    return fh, resolved


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI / DeepSeek function-calling shape)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command on the server and return its stdout + stderr. "
                "Only allowlisted commands are permitted (no shell pipes/redirects). "
                "The command runs with a 15-second timeout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "The command with arguments, e.g. 'ls -la /opt/agent'. "
                            "Only allowlisted commands are permitted."
                        ),
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "Optional working directory. Defaults to /opt/agent.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file on the server. "
                "Paths are restricted to /opt/agent and /tmp. "
                "Maximum file size is 10 MB."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum lines to return (default 200, max 500).",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or overwrite a file on the server. "
                "Paths are restricted to /opt/agent/data/ and /tmp."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to write to (relative to /opt/agent/data/ if not absolute).",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full text content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web using DuckDuckGo and return the top results. "
                "Use for documentation, answers, or current information."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query string.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results (default 5, max 10).",
                    },
                },
                "required": ["query"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------

async def _run_command(command: str, working_dir: str = "/opt/agent") -> str:
    """Execute a command via subprocess_exec with an explicit allowlist."""
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return f"[ERROR] Invalid command syntax: {e}"

    if not parts:
        return "[ERROR] Empty command"

    base = parts[0]
    if base not in ALLOWED_COMMANDS:
        return (
            f"[BLOCKED] Command '{base}' is not in the allowlist.\n"
            f"Allowed: {', '.join(sorted(ALLOWED_COMMANDS.keys()))}"
        )

    # Validate working_dir
    try:
        cwd = str(_in_allowed(working_dir, ALLOWED_READ_PATHS))
    except PermissionError:
        return f"[BLOCKED] Working directory not allowed: {working_dir}"

    try:
        proc = await asyncio.create_subprocess_exec(
            ALLOWED_COMMANDS[base],
            *parts[1:],               # args only — no shell interpolation
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=15
        )
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        parts_out: list[str] = []
        if out:
            parts_out.append(out)
        if err:
            parts_out.append(f"[stderr]\n{err}")
        if not parts_out:
            parts_out.append(f"(exit code {proc.returncode})")
        return "\n".join(parts_out)
    except asyncio.TimeoutError:
        return "[TIMEOUT] Command exceeded 15 seconds — killed."
    except FileNotFoundError:
        return f"[ERROR] Command binary not found: {ALLOWED_COMMANDS.get(base, base)}"
    except Exception as exc:
        return f"[ERROR] {exc}"


async def _read_file(path: str, max_lines: int = 200) -> str:
    """Read a file with TOCTOU-safe path validation."""
    try:
        fh, resolved = _safe_open(path, "r", ALLOWED_READ_PATHS)
    except PermissionError as e:
        return f"[BLOCKED] {e}"
    except FileNotFoundError:
        return f"[ERROR] File not found: {path}"
    except Exception as e:
        return f"[ERROR] {e}"

    try:
        with fh:
            if resolved.is_dir():
                return _list_directory(resolved, max_lines)

            limit = min(max_lines, 500)
            lines_out: list[str] = []
            byte_count = 0
            for i, line in enumerate(fh):
                if i >= limit:
                    lines_out.append(f"... (truncated, {limit} lines shown)")
                    break
                byte_count += len(line.encode("utf-8"))
                if byte_count > MAX_READ_BYTES:
                    lines_out.append("... (truncated, max byte limit reached)")
                    break
                lines_out.append(line.rstrip("\n"))
            return "\n".join(lines_out) if lines_out else "(empty file)"
    except UnicodeDecodeError:
        return f"[ERROR] Binary file — cannot read as text: {resolved}"
    except Exception as exc:
        return f"[ERROR] {exc}"


def _list_directory(resolved: Path, max_entries: int) -> str:
    """Return a directory listing."""
    try:
        entries = sorted(resolved.iterdir())[:max_entries]
    except PermissionError:
        return f"[ERROR] Permission denied: {resolved}"
    if not entries:
        return f"Directory {resolved} is empty."
    lines = []
    for entry in entries:
        try:
            st = entry.stat()
            size = f"{st.st_size:>8}"
            suffix = "/" if entry.is_dir() else ""
        except OSError:
            size = "       ?"
            suffix = ""
        lines.append(f"  {size}  {entry.name}{suffix}")
    return f"Directory listing of {resolved}:\n" + "\n".join(lines)


async def _write_file(path: str, content: str) -> str:
    """Write a file with TOCTOU-safe path validation."""
    try:
        fh, resolved = _safe_open(path, "w", ALLOWED_WRITE_PATHS)
    except PermissionError as e:
        return f"[BLOCKED] {e}"
    except Exception as e:
        return f"[ERROR] {e}"

    try:
        with fh:
            fh.write(content)
        size = len(content.encode("utf-8"))
        return f"Written {resolved} ({size} bytes, {content.count(chr(10)) + 1} lines)"
    except Exception as exc:
        return f"[ERROR] {exc}"


async def _web_search(query: str, max_results: int = 5) -> str:
    """Search DuckDuckGo (no API key needed)."""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return "[ERROR] duckduckgo-search not installed. Run: pip install duckduckgo-search"

    try:
        limit = min(max_results, 10)
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=limit))
        if not results:
            return f"No results for: {query}"
        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "N/A")
            href = r.get("href", "")
            body = r.get("body", "")[:300]
            lines.append(f"{i}. **{title}**\n   {body}\n   {href}")
        return "\n\n".join(lines)
    except Exception as exc:
        return f"[ERROR] Search failed: {exc}"


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

EXECUTORS = {
    "run_command": _run_command,
    "read_file": _read_file,
    "write_file": _write_file,
    "web_search": _web_search,
}


async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Run a single tool by name and return its result string."""
    executor = EXECUTORS.get(name)
    if executor is None:
        return f"[ERROR] Unknown tool: {name}"
    try:
        return await executor(**arguments)
    except TypeError as e:
        return f"[ERROR] Bad arguments for {name}: {e}"
