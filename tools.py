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
    {
        "type": "function",
        "function": {
            "name": "create_pptx",
            "description": (
                "创建 PowerPoint 演示文稿（.pptx），保存到 workspace。"
                "支持标题页、目录、正文、两栏、结束页等布局。"
                "支持代码块、粗体/斜体标记。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "输出文件名（例如 'presentation.pptx'），保存到 workspace。",
                    },
                    "title": {
                        "type": "string",
                        "description": "演示文稿主标题。",
                    },
                    "subtitle": {
                        "type": "string",
                        "description": "副标题/作者（可选）。",
                    },
                    "slides": {
                        "type": "array",
                        "description": "幻灯片数组。每项含 title(标题), content(内容/要点，用\\n分隔), layout(title|bullets|text|two_column|quote|end)。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "幻灯片标题。"},
                                "content": {"type": "string", "description": "内容文本，用 \\n 分行，支持 **粗体** 和 *斜体*。"},
                                "layout": {"type": "string", "description": "布局：title(封面) | toc(目录) | bullets(要点) | text(正文) | two_column(两栏，用 ==== 分左右) | quote(引用) | end(结束页)。"},
                            },
                        },
                    },
                },
                "required": ["filename", "title", "slides"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_pdf",
            "description": (
                "创建 PDF 文档（使用 fpdf2），保存到 workspace。"
                "支持标题、正文、代码块、分页。支持中文字体。"
                "适合生成报告、笔记、文档等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "输出文件名（例如 'report.pdf'），保存到 workspace。",
                    },
                    "title": {
                        "type": "string",
                        "description": "文档标题（显示在封面和页眉）。",
                    },
                    "author": {
                        "type": "string",
                        "description": "作者名（可选）。",
                    },
                    "sections": {
                        "type": "array",
                        "description": "章节数组。每项含 heading（标题）和 content（正文，支持 \\n 分段）。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string", "description": "章节标题。"},
                                "content": {"type": "string", "description": "正文内容，用 \\n 分段。支持 **粗体** 标记。"},
                            },
                        },
                    },
                },
                "required": ["filename", "title", "sections"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "抓取网页内容并转为 Markdown 文本。用于查阅在线文档、"
                "阅读文章、获取最新信息。自动提取正文，过滤导航/广告。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要抓取的网页 URL（必须 http/https）。",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最大返回字符数（默认 8000）。",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "code_exec",
            "description": (
                "在沙箱中执行 Python 代码并返回 stdout。"
                "限制：5秒超时、禁止网络/文件系统写入/子进程/os模块。"
                "适合计算、数据处理、算法验证。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "要执行的 Python 代码。",
                    },
                },
                "required": ["code"],
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


async def _create_pptx(filename: str, title: str, slides: list[dict], subtitle: str = "") -> str:
    """Generate a .pptx file with structured slides."""

    # Validate filename
    if not filename.endswith(".pptx"):
        filename += ".pptx"
    # Only allow safe characters
    safe_name = re.sub(r"[^\w\-.]", "_", filename)
    output_path = f"/opt/agent/workspace/{safe_name}"

    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt, Emu
        from pptx.dml.color import RGBColor
        from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    except ImportError:
        return "[ERROR] python-pptx not installed. Run: pip install python-pptx"

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # ---------- color scheme ----------
    BG_DARK   = RGBColor(0x0D, 0x11, 0x17)
    ACCENT    = RGBColor(0x58, 0xA6, 0xFF)
    WHITE     = RGBColor(0xFF, 0xFF, 0xFF)
    DIM       = RGBColor(0x8B, 0x94, 0x9E)
    GREEN     = RGBColor(0x3F, 0xB9, 0x50)

    def set_slide_bg(slide, color):
        bg = slide.background
        fill = bg.fill
        fill.solid()
        fill.fore_color.rgb = color

    def add_textbox(slide, left, top, width, height, text, font_size=18, bold=False, color=WHITE, alignment=PP_ALIGN.LEFT, font_name="Arial"):
        txBox = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        tf = txBox.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = text
        p.font.size = Pt(font_size)
        p.font.bold = bold
        p.font.color.rgb = color
        p.font.name = font_name
        p.alignment = alignment
        return tf

    def add_rich_textbox(slide, left, top, width, height, content, font_size=16, color=WHITE):
        """Add a textbox with simple markdown-like formatting (**bold**, *italic*, `code`)."""
        txBox = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        tf = txBox.text_frame
        tf.word_wrap = True
        tf.clear()

        lines = content.split("\n")
        for i, line in enumerate(lines):
            if i > 0:
                p = tf.add_paragraph()
            else:
                p = tf.paragraphs[0]

            p.space_after = Pt(6)
            p.alignment = PP_ALIGN.LEFT

            # Parse inline formatting: **bold**, *italic*, `code`
            parts = re.split(r"(\*\*.*?\*\*|\*.*?\*|`.*?`)", line)
            for part in parts:
                if part.startswith("**") and part.endswith("**"):
                    run = p.add_run()
                    run.text = part[2:-2]
                    run.font.bold = True
                    run.font.size = Pt(font_size)
                    run.font.color.rgb = color
                    run.font.name = "Arial"
                elif part.startswith("*") and part.endswith("*") and not part.startswith("**"):
                    run = p.add_run()
                    run.text = part[1:-1]
                    run.font.italic = True
                    run.font.size = Pt(font_size)
                    run.font.color.rgb = color
                    run.font.name = "Arial"
                elif part.startswith("`") and part.endswith("`"):
                    run = p.add_run()
                    run.text = part[1:-1]
                    run.font.size = Pt(font_size - 1)
                    run.font.color.rgb = GREEN
                    run.font.name = "Consolas"
                elif part:
                    run = p.add_run()
                    run.text = part
                    run.font.size = Pt(font_size)
                    run.font.color.rgb = color
                    run.font.name = "Arial"

        return tf

    # ---------- slide builders ----------
    def slide_title():
        sl = prs.slides.add_slide(prs.slide_layouts[6])  # blank
        set_slide_bg(sl, BG_DARK)
        # accent line
        line = sl.shapes.add_shape(1, Inches(1.5), Inches(2.8), Inches(2), Pt(4))  # rectangle
        line.fill.solid()
        line.fill.fore_color.rgb = ACCENT
        line.line.fill.background()
        add_textbox(sl, 1.5, 3.0, 10.3, 1.5, title, font_size=44, bold=True, color=WHITE)
        if subtitle:
            add_textbox(sl, 1.5, 4.3, 10.3, 0.6, subtitle, font_size=20, color=DIM)
        add_textbox(sl, 1.5, 6.6, 5, 0.4, "Agent 制作", font_size=12, color=DIM)

    def slide_toc():
        sl = prs.slides.add_slide(prs.slide_layouts[6])
        set_slide_bg(sl, BG_DARK)
        add_textbox(sl, 1.5, 0.6, 10, 0.6, "目录", font_size=32, bold=True, color=WHITE)
        add_textbox(sl, 1.5, 1.3, 2, 0.3, "─" * 15, font_size=14, color=ACCENT)
        # extract slide titles
        items = [s.get("title", "") for s in slides if s.get("layout") not in ("title", "toc", "end")]
        y = 1.8
        for i, item in enumerate(items, 1):
            add_textbox(sl, 2.0, y, 9, 0.5, f"{i:02d}  {item}", font_size=20, color=DIM if i % 2 == 0 else WHITE)
            y += 0.5
        add_textbox(sl, 1.5, 6.6, 5, 0.4, "Agent 制作", font_size=12, color=DIM)

    def slide_bullets(s):
        sl = prs.slides.add_slide(prs.slide_layouts[6])
        set_slide_bg(sl, BG_DARK)
        add_textbox(sl, 1.5, 0.6, 10, 0.6, s.get("title", ""), font_size=32, bold=True, color=WHITE)
        add_textbox(sl, 1.5, 1.3, 2, 0.3, "─" * 15, font_size=14, color=ACCENT)
        # bullet points
        content = s.get("content", "")
        bullets = [b.strip() for b in content.split("\n") if b.strip()]
        y = 1.8
        for b in bullets:
            add_rich_textbox(sl, 2.0, y, 9.5, 0.6, b, font_size=18)
            y += 0.55

    def slide_text(s):
        sl = prs.slides.add_slide(prs.slide_layouts[6])
        set_slide_bg(sl, BG_DARK)
        add_textbox(sl, 1.5, 0.6, 10, 0.6, s.get("title", ""), font_size=32, bold=True, color=WHITE)
        add_textbox(sl, 1.5, 1.3, 2, 0.3, "─" * 15, font_size=14, color=ACCENT)
        add_rich_textbox(sl, 1.5, 1.8, 10.3, 4.8, s.get("content", ""), font_size=16)

    def slide_two_column(s):
        sl = prs.slides.add_slide(prs.slide_layouts[6])
        set_slide_bg(sl, BG_DARK)
        add_textbox(sl, 1.5, 0.6, 10, 0.6, s.get("title", ""), font_size=32, bold=True, color=WHITE)
        add_textbox(sl, 1.5, 1.3, 2, 0.3, "─" * 15, font_size=14, color=ACCENT)
        content = s.get("content", "")
        parts = content.split("====", 1)
        left_text = parts[0].strip() if len(parts) > 0 else ""
        right_text = parts[1].strip() if len(parts) > 1 else ""
        # vertical divider
        div = sl.shapes.add_shape(1, Inches(6.6), Inches(1.8), Pt(2), Inches(4.2))
        div.fill.solid()
        div.fill.fore_color.rgb = ACCENT
        div.line.fill.background()
        add_rich_textbox(sl, 1.5, 1.8, 4.8, 4.8, left_text, font_size=16)
        add_rich_textbox(sl, 7.0, 1.8, 4.8, 4.8, right_text, font_size=16)

    def slide_quote(s):
        sl = prs.slides.add_slide(prs.slide_layouts[6])
        set_slide_bg(sl, BG_DARK)
        content = s.get("content", "").strip()
        add_rich_textbox(sl, 2.5, 2.5, 8.3, 2.5, f"「{content}」", font_size=28, color=WHITE)
        title = s.get("title", "").strip()
        if title:
            add_textbox(sl, 2.5, 5.0, 8.3, 0.5, f"— {title}", font_size=16, color=DIM)

    def slide_end():
        sl = prs.slides.add_slide(prs.slide_layouts[6])
        set_slide_bg(sl, BG_DARK)
        add_textbox(sl, 2, 3.0, 9, 1, "谢谢", font_size=52, bold=True, color=WHITE, alignment=PP_ALIGN.CENTER)
        add_textbox(sl, 2, 4.2, 9, 0.6, "Agent 制作", font_size=16, color=DIM, alignment=PP_ALIGN.CENTER)

    # ---------- render ----------
    layout_map = {
        "title": slide_title,
        "toc": slide_toc,
        "bullets": slide_bullets,
        "text": slide_text,
        "two_column": slide_two_column,
        "quote": slide_quote,
        "end": slide_end,
    }

    rendered_one_cover = False
    for s in slides:
        layout = s.get("layout", "bullets")
        # auto-insert cover if first slide isn't title
        if not rendered_one_cover and layout != "title":
            slide_title()
            rendered_one_cover = True
        if layout == "title":
            rendered_one_cover = True

        builder = layout_map.get(layout, slide_bullets)
        builder(s)

    # always end with end slide
    slide_end()

    try:
        prs.save(output_path)
        return f"PPT 已保存: {output_path} ({len(slides) + 2} 页，含封面和结束页)"
    except Exception as e:
        return f"[ERROR] 保存失败: {e}"


async def _create_pdf(filename: str, title: str, sections: list[dict], author: str = "") -> str:
    """Generate a PDF document with structured sections."""

    if not filename.endswith(".pdf"):
        filename += ".pdf"
    safe_name = re.sub(r"[^\w\-.]", "_", filename)
    output_path = f"/opt/agent/workspace/{safe_name}"

    try:
        from fpdf import FPDF
    except ImportError:
        return "[ERROR] fpdf2 not installed. Run: pip install fpdf2"

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)

    # ---------- helpers ----------
    def add_page():
        pdf.add_page()

    def write_title(txt):
        pdf.set_font("Helvetica", "B", 22)
        pdf.multi_cell(0, 12, txt, align="C")
        pdf.ln(4)

    def write_heading(txt):
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 14)
        # Underline with a line
        y = pdf.get_y()
        pdf.set_draw_color(88, 166, 255)  # accent blue
        pdf.set_line_width(0.5)
        pdf.line(20, y + 8, 190, y + 8)
        pdf.multi_cell(0, 10, txt)
        pdf.ln(2)

    def write_body(txt):
        pdf.set_font("Helvetica", "", 11)
        # Parse **bold** markers
        parts = re.split(r"(\*\*.*?\*\*)", txt)
        for part in parts:
            if part.startswith("**") and part.endswith("**"):
                pdf.set_font("Helvetica", "B", 11)
                pdf.write(5.5, part[2:-2])
            else:
                pdf.set_font("Helvetica", "", 11)
                pdf.write(5.5, part)
        pdf.ln(2)

    def write_code(txt):
        pdf.set_fill_color(240, 240, 245)
        pdf.set_font("Courier", "", 9)
        for line in txt.split("\n"):
            pdf.set_x(25)
            pdf.cell(160, 5, line, fill=True)
            pdf.ln()

    # ---------- render ----------
    # Cover
    add_page()
    pdf.ln(30)
    write_title(title)
    if author:
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 10, author, align="C")
        pdf.ln(14)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(139, 148, 158)
    pdf.cell(0, 10, f"Agent 生成  |  {len(sections)} 个章节", align="C")

    # Sections
    for sec in sections:
        add_page()
        h = sec.get("heading", "")
        c = sec.get("content", "")
        write_heading(h)
        pdf.ln(4)

        # Split content into paragraphs
        paragraphs = c.split("\n")
        in_code = False
        code_buf = []
        for para in paragraphs:
            stripped = para.strip()
            if stripped.startswith("```"):
                if in_code:
                    write_code("\n".join(code_buf))
                    code_buf = []
                    in_code = False
                else:
                    in_code = True
            elif in_code:
                code_buf.append(para)
            elif stripped:
                write_body(stripped)
            else:
                pdf.ln(4)  # blank line = paragraph break

        if in_code and code_buf:
            write_code("\n".join(code_buf))

    try:
        pdf.output(output_path)
        return f"PDF 已保存: {output_path} ({pdf.pages_count} 页)"
    except Exception as e:
        return f"[ERROR] PDF 保存失败: {e}"


async def _web_fetch(url: str, max_chars: int = 8000) -> str:
    """Fetch a web page and return its main text content as Markdown."""
    if not url.startswith(("http://", "https://")):
        return "[ERROR] URL must start with http:// or https://"

    # Block internal/private IPs to prevent SSRF
    import ipaddress
    from urllib.parse import urlparse

    hostname = urlparse(url).hostname
    if hostname:
        # Block localhost and private ranges
        blocked = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
        if hostname in blocked:
            return "[BLOCKED] Cannot fetch localhost URLs"
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return f"[BLOCKED] Cannot fetch private/internal IP: {hostname}"
        except ValueError:
            pass  # not an IP, probably a domain

    try:
        import httpx
    except ImportError:
        return "[ERROR] httpx not installed. Run: pip install httpx"

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cl:
            resp = await cl.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; AgentBot/1.0)"},
            )
            resp.raise_for_status()

        html = resp.text

        # Try to extract readable content
        try:
            from markdownify import markdownify as md
            text = md(html, heading_style="ATX", strip=["script", "style", "nav", "footer", "header"])
        except ImportError:
            # Fallback: use BeautifulSoup
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)

        limit = min(max_chars, 20000)
        if len(text) > limit:
            text = text[:limit] + f"\n\n... (截断，原文 {len(text)} 字符)"

        return text if text.strip() else "(页面无文本内容)"

    except httpx.HTTPStatusError as e:
        return f"[ERROR] HTTP {e.response.status_code}"
    except httpx.TimeoutException:
        return "[TIMEOUT] 请求超时 (15s)"
    except Exception as e:
        return f"[ERROR] {e}"


async def _code_exec(code: str) -> str:
    """Execute Python code in a restricted sandbox subprocess."""
    if not code or not code.strip():
        return "[ERROR] Empty code"

    # Sandbox preamble: restrict dangerous operations
    sandbox_preamble = """
import builtins
__builtins__ = {k: v for k, v in builtins.__dict__.items()
    if k not in ('open', 'exec', 'eval', 'compile', 'input',
                  '__import__', 'breakpoint')}
import sys, os
sys.modules['os'] = None
sys.modules['subprocess'] = None
sys.modules['socket'] = None
sys.modules['requests'] = None
sys.modules['urllib'] = None
sys.modules['http'] = None
sys.modules['ftplib'] = None
sys.modules['smtplib'] = None
sys.modules['telnetlib'] = None
del sys, os
"""

    full_code = sandbox_preamble + "\n" + code

    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/python3", "-c", full_code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err[:500]}")
        if not parts:
            parts.append("(no output)")
        parts.append(f"(exit: {proc.returncode})")
        return "\n".join(parts)

    except asyncio.TimeoutError:
        return "[TIMEOUT] Code execution exceeded 5 seconds — killed."
    except FileNotFoundError:
        return "[ERROR] python3 not found"
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
    "create_pptx": _create_pptx,
    "create_pdf": _create_pdf,
    "web_fetch": _web_fetch,
    "code_exec": _code_exec,
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
