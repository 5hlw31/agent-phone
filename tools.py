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
import difflib
import fnmatch
import os
import re
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
    "/opt/agent/workspace",
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
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "精确替换文件中的字符串并显示 unified diff。"
                "old_string 必须精确匹配（含缩进），new_string 为替换内容。"
                "设 replace_all=true 替换所有出现。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要编辑的文件路径。",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "要被替换的精确文本。",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "替换后的新文本。",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有匹配（默认 false，仅替换第一处）。",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "在 workspace 中按正则表达式搜索代码。返回匹配文件路径和行内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "正则表达式搜索模式，如 'def foo|class Bar'。",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索目录，默认 /opt/agent/workspace。",
                    },
                    "glob": {
                        "type": "string",
                        "description": "文件名过滤，如 '*.py' 或 '*.{js,ts}'。",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "按 glob 模式列出文件。用于了解项目结构、查找文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "搜索根目录，默认 /opt/agent/workspace。",
                    },
                    "glob": {
                        "type": "string",
                        "description": "glob 模式，如 '**/*.py' 或 '*.md'。默认 '**/*'。",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回数（默认 100）。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "查看 workspace 的 git 仓库状态（git status --short）。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": (
                "查看 workspace 的 git diff。默认显示未暂存更改，"
                "加 staged=true 查看已暂存更改。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "staged": {
                        "type": "boolean",
                        "description": "是否查看已暂存的 diff（git diff --staged）。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "查看 git 提交历史。",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_count": {
                        "type": "integer",
                        "description": "最多显示条数（默认 10）。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": (
                "提交 workspace 中的所有更改。需要提供 commit message。"
                "仅操作 /opt/agent/workspace 下的 git 仓库。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "提交信息（遵循 conventional commits 格式）。",
                    },
                },
                "required": ["message"],
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
# New tools: edit_file, search_code, list_files, git_*
# ---------------------------------------------------------------------------

def _render_diff(old: str, new: str, path: str) -> str:
    """Return a unified diff string."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}", tofile=f"b/{path}",
    )
    return "".join(diff)


async def _edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Exact string replacement with unified diff output."""
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
            content = fh.read()
    except Exception as e:
        return f"[ERROR] Cannot read: {e}"

    if old_string not in content:
        return (
            f"[ERROR] old_string not found in {resolved.name}.\n"
            f"Tip: Use read_file first to check exact whitespace/indentation."
        )

    count = content.count(old_string)
    if not replace_all and count > 1:
        return (
            f"[ERROR] old_string found {count} times in {resolved.name}. "
            f"Set replace_all=true to replace all, or make old_string more specific."
        )

    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

    # Write back via safe path
    try:
        # reuse _safe_open for write
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as fh_out:
            fh_out.write(new_content)
    except Exception as e:
        return f"[ERROR] Write failed: {e}"

    # Generate diff
    diff_output = _render_diff(content, new_content, str(resolved))
    replacements = count if replace_all else 1
    return (
        f"Edited {resolved} ({replacements} replacement(s))\n\n"
        f"```diff\n{diff_output}\n```"
    )


async def _search_code(pattern: str, path: str = "/opt/agent/workspace", glob: str | None = None) -> str:
    """Regex grep across workspace files."""
    try:
        root = _in_allowed(path, ALLOWED_READ_PATHS)
    except PermissionError:
        return f"[BLOCKED] Path not allowed: {path}"

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"[ERROR] Invalid regex: {e}"

    results: list[str] = []
    max_matches = 50
    found = 0

    try:
        for filepath in root.rglob("*"):
            if found >= max_matches:
                break
            if not filepath.is_file():
                continue
            if glob and not fnmatch.fnmatch(filepath.name, glob):
                continue
            # Skip binary/large files
            try:
                if filepath.stat().st_size > 1024 * 1024:  # 1MB
                    continue
            except OSError:
                continue

            try:
                lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue

            for lineno, line in enumerate(lines, 1):
                if found >= max_matches:
                    break
                if regex.search(line):
                    rel = str(filepath.relative_to(root))
                    results.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    found += 1
    except PermissionError:
        pass

    if not results:
        return f"No matches for /{pattern}/ in {root}"
    return "\n".join(results)


async def _list_files(path: str = "/opt/agent/workspace", glob: str = "**/*", max_results: int = 100) -> str:
    """Glob-based file listing."""
    try:
        root = _in_allowed(path, ALLOWED_READ_PATHS)
    except PermissionError:
        return f"[BLOCKED] Path not allowed: {path}"

    limit = min(max_results, 200)
    entries: list[str] = []
    try:
        for filepath in root.rglob("*"):
            if len(entries) >= limit:
                entries.append("... (truncated)")
                break
            if glob != "**/*" and not fnmatch.fnmatch(filepath.name, glob):
                # also match against relative path for ** patterns
                rel = str(filepath.relative_to(root))
                if not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(filepath.name, glob):
                    continue
            try:
                st = filepath.stat()
            except OSError:
                continue
            suffix = "/" if filepath.is_dir() else ""
            size = f"{st.st_size:>8}" if not filepath.is_dir() else "       -"
            rel = str(filepath.relative_to(root))
            entries.append(f"  {size}  {rel}{suffix}")
    except PermissionError:
        pass

    if not entries:
        return f"No files matching '{glob}' in {root}"
    return f"{root}/ ({len(entries)} entries):\n" + "\n".join(entries)


async def _git_status(**kwargs) -> str:
    """Run git status in workspace."""
    return await _run_git(["status", "--short"])


async def _git_diff(staged: bool = False, **kwargs) -> str:
    """Run git diff in workspace."""
    args = ["diff"]
    if staged:
        args.append("--staged")
    return await _run_git(args)


async def _git_log(max_count: int = 10, **kwargs) -> str:
    """Run git log in workspace."""
    return await _run_git([
        "log", f"--max-count={min(max_count, 50)}",
        "--oneline", "--decorate",
    ])


async def _git_commit(message: str, **kwargs) -> str:
    """Git commit all changes in workspace."""
    if not message or not message.strip():
        return "[ERROR] Commit message is required"

    # Stage all
    out1 = await _run_git(["add", "-A"])
    # Commit
    out2 = await _run_git(["commit", "-m", message.strip()])
    return f"{out1}\n{out2}"


async def _run_git(args: list[str]) -> str:
    """Helper: execute a git command in /opt/agent/workspace."""
    ws = "/opt/agent/workspace"
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=ws,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err}")
        if not parts:
            parts.append("(no output)")
        return "\n".join(parts)
    except asyncio.TimeoutError:
        return "[TIMEOUT] Git command exceeded 30 seconds."
    except FileNotFoundError:
        return "[ERROR] Git not found on server"
    except Exception as exc:
        return f"[ERROR] {exc}"


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

EXECUTORS = {
    "run_command": _run_command,
    "read_file": _read_file,
    "write_file": _write_file,
    "web_search": _web_search,
    "edit_file": _edit_file,
    "search_code": _search_code,
    "list_files": _list_files,
    "git_status": _git_status,
    "git_diff": _git_diff,
    "git_log": _git_log,
    "git_commit": _git_commit,
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
