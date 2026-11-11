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
import json
import logging
import os
import re
import shlex
import signal
import stat as stat_module
from pathlib import Path
from typing import Any
from workspace import Workspace
from change_journal import observe_write
import weakref
import contextvars
from contextlib import contextmanager
import importlib.util

logger = logging.getLogger("agent-phone.tools")

# ---------------------------------------------------------------------------
# Safety configuration
# ---------------------------------------------------------------------------

WORKSPACE_ROOT = Path(os.getenv("AGENT_WORKSPACE", str(Path(__file__).resolve().parent / "workspace"))).resolve()
_CURRENT_WORKSPACE = contextvars.ContextVar('agent_workspace', default=Workspace(WORKSPACE_ROOT))


class _WorkspaceProxy:
    def __getattr__(self, name):
        return getattr(_CURRENT_WORKSPACE.get(), name)


WS = _WorkspaceProxy()


@contextmanager
def using_workspace(workspace):
    token = _CURRENT_WORKSPACE.set(workspace)
    try:
        yield
    finally:
        _CURRENT_WORKSPACE.reset(token)

# Maximum file size for reads (10 MB)
MAX_READ_BYTES = 10 * 1024 * 1024

# File paths are checked by the same Workspace policy used by HTTP routes.

# ---------------------------------------------------------------------------
# Path helpers (TOCTOU-safe)
# ---------------------------------------------------------------------------

def _safe_open(path: str):
    return WS.open_text(path), WS.path(path)


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
                "The command runs with a 15-second timeout. "
                "File-reading commands (cat, grep, etc.) are restricted to /opt/agent and /tmp. "
                "For web requests use web_fetch (SSRF-protected) instead of curl/wget."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "The command with arguments, e.g. 'ls -la'. "
                            "Only allowlisted commands are permitted."
                        ),
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "Optional directory inside the workspace. Defaults to the workspace root.",
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
                "Only files inside the configured workspace are accessible. "
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
                "Files are saved atomically inside the configured workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace root.",
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
                        "description": "搜索目录，默认工作区根目录。",
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
                        "description": "搜索根目录，默认工作区根目录。",
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

async def _communicate_or_stop(proc, timeout: float, max_output_bytes: int = 65536):
    """Capture bounded output from an owned process group; reap on every error."""
    async def read(reader):
        pieces=[];size=0
        while True:
            part=await reader.read(8192)
            if not part:
                return b''.join(pieces)
            size+=len(part)
            if size>max_output_bytes:
                raise ValueError('工具输出超过读取限制。')
            pieces.append(part)
    stdout=asyncio.create_task(read(proc.stdout))
    stderr=asyncio.create_task(read(proc.stderr))
    waiter=asyncio.create_task(proc.wait())
    output=asyncio.gather(stdout,stderr,waiter)
    try:
        result=await asyncio.wait_for(asyncio.shield(output),timeout)
        return result[0],result[1]
    except BaseException:
        try:
            if os.name=='posix':
                os.killpg(proc.pid,signal.SIGKILL)
            elif proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
        except OSError:
            if proc.returncode is None:
                proc.kill()
        for task in (stdout,stderr):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout,stderr,return_exceptions=True)
        async def discard(reader):
            while await reader.read(8192):
                pass
        try:
            await asyncio.wait_for(asyncio.gather(discard(proc.stdout),discard(proc.stderr),waiter),3)
        except TimeoutError:
            # Closing pipes is a last resort if an unrelated descendant kept them open.
            for reader in (proc.stdout,proc.stderr):
                transport=getattr(reader,'_transport',None)
                if transport:
                    transport.close()
            await proc.wait()
        await asyncio.gather(output,return_exceptions=True)
        raise


async def _run_command(command: str, working_dir: str = "") -> str:
    """A small workspace command adapter, never an unrestricted host shell."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        parts = list(lexer)
        if not parts:
            return "[ERROR] Empty command"
        if any(set(part) <= set(";&|<>") for part in parts):
            return "[ERROR] 请逐条运行命令，不支持 shell 组合或重定向。"
        cwd = WS.path(working_dir)
        base, args = parts[0], parts[1:]
        def file_path(value):
            return str(WS.path(str(cwd / value) if not Path(value).is_absolute() else value))
        if base == "pwd" and not args:
            return str(cwd)
        if base == "echo":
            return " ".join(args)
        if base == "ls":
            paths = [arg for arg in args if not arg.startswith('-')]
            if any(arg.startswith('-') and arg not in ('-l','-a','-la','-al') for arg in args) or len(paths)>1:
                return "[ERROR] ls 只支持 -l、-a 和一个工作区目录。"
            return _list_directory(Path(file_path(paths[0])) if paths else cwd, 200)
        if base in ("cat", "head", "tail", "wc"):
            count = 100 if base == "cat" else 10
            if base in ("head", "tail") and len(args)>=2 and args[0] in ('-n','--lines'):
                count = max(1, min(int(args[1]),500)); args=args[2:]
            wc_mode = '-l'
            if base == 'wc' and args and args[0] in ('-l','-w','-c'):
                wc_mode=args.pop(0)
            if len(args)!=1 or args[0].startswith('-'):
                return "[ERROR] 请提供一个工作区文件；复杂查找请用专用工具。"
            text = WS.read_text(file_path(args[0]))
            if base == 'wc':
                value = len(text.splitlines()) if wc_mode=='-l' else len(text.split()) if wc_mode=='-w' else len(text.encode('utf-8'))
                return str(value)
            lines=text.splitlines()
            return '输出：\n'+('\n'.join(lines[-count:] if base=='tail' else lines[:count]) or '(empty file)')
        return "[BLOCKED] 当前命令工具支持工作区内的 ls、cat、head、tail、wc、pwd、echo。文件修改请用专用工具。"
    except PermissionError as exc:
        return f"[BLOCKED] {exc}"
    except (OSError, ValueError) as exc:
        return f"[ERROR] {exc}"


async def _read_file(path: str, max_lines: int = 200, start_line: int = 1) -> str:
    try:
        resolved=WS.path(path)
        if resolved.is_dir():
            return _list_directory(resolved,max_lines)
        source=resolved.relative_to(WS.root)
        document=resolved.suffix.lower() in ('.pdf','.docx')
        if document:
            if not resolved.exists():return f'[ERROR] 文件不存在：{path}'
            path=str(source)+'.text.md'
            if not WS.path(path).is_file():
                return '[ERROR] 此文档没有提取到可读取的正文。扫描件暂不支持 OCR，请用上传入口处理文字版文档。'
            resolved=WS.path(path)
        with WS.open_text(path) as file:
            selected=[];last=0;more=False
            for number,line in enumerate(file,1):
                last=number
                if number<start_line:
                    continue
                if len(selected)>=max_lines:
                    more=True;break
                selected.append(line.rstrip('\n'))
            if not selected:
                return '(empty file)' if last==0 else '[ERROR] 起始行超过文件总行数。'
            result='\n'.join(selected)
            if more:
                result+=f'\n[后续内容可从第 {start_line+len(selected)} 行继续读取]'
            header=f'文档提取正文：{source}（不包含图片；请核对原文件）' if document else f'文件：{resolved.relative_to(WS.root)}'
            return f'{header} · 从第 {start_line} 行读取\n\n'+result
    except PermissionError as exc:
        return f'[BLOCKED] {exc}'
    except FileNotFoundError:
        return f'[ERROR] 文件不存在：{path}'
    except (OSError,ValueError) as exc:
        return f'[ERROR] {exc}'


def _list_directory(resolved: Path, max_entries: int) -> str:
    """Return a directory listing."""
    try:
        entries = [WS.path(entry["path"]) for entry in WS.listing(str(resolved))[:max(1,max_entries)]]
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


async def _write_file(path: str, content: str, overwrite: bool = False) -> str:
    if Path(path).suffix.lower() in ('.pdf','.docx'):
        return '[ERROR] 不支持直接写入 PDF 或 Word 原文件；请另存为 Markdown 文本。'
    try:
        if WS.path(path).exists() and not overwrite:
            return '[ERROR] 文件已经存在。修改请用 edit_file；确需完整覆盖时显式设置 overwrite。'
        WS.write_text(path, content)
        return f"Written {WS.path(path)} ({len(content.encode('utf-8'))} bytes)"
    except PermissionError as exc:
        return f"[BLOCKED] {exc}"
    except (OSError, ValueError) as exc:
        return f"[ERROR] {exc}"


async def _web_search(query: str, max_results: int = 5) -> str:
    def search():
        from ddgs import DDGS
        with DDGS(timeout=8) as ddgs:
            return list(ddgs.text(query,max_results=min(max_results,10)))
    try:
        async with asyncio.timeout(12):
            results=await asyncio.to_thread(search)
        return '\n\n'.join(f"{i}. {r.get('title','')}\n{r.get('href','')}\n{str(r.get('body',''))[:350]}" for i,r in enumerate(results,1)) or '没有找到搜索结果。'
    except TimeoutError:
        return '[TIMEOUT] 搜索超时。'
    except Exception:
        logger.exception('Web search failed')
        return '[ERROR] 搜索暂时不可用，可以直接提供网页地址。'

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
    if Path(path).suffix.lower() in ('.pdf','.docx'):
        return '[ERROR] 文档原文件不可直接编辑；请修改提取正文副本或另存为 Markdown。'
    if not old_string:
        return '[ERROR] 要替换的原文不能为空。'
    try:
        fh, resolved = _safe_open(path)
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
        WS.write_text(str(resolved), new_content)
    except Exception as e:
        return f"[ERROR] Write failed: {e}"

    # Generate diff
    diff_output = _render_diff(content, new_content, str(resolved))
    replacements = count if replace_all else 1
    return (
        f"Edited {resolved} ({replacements} replacement(s))\n\n"
        f"```diff\n{diff_output}\n```"
    )


def _matches_glob(name,relative,pattern):
    return fnmatch.fnmatch(name,pattern) or fnmatch.fnmatch(relative,pattern) or (pattern.startswith('**/') and fnmatch.fnmatch(relative,pattern[3:]))


async def _search_code(pattern: str, path: str = "", glob: str | None = None) -> str:
    """Bounded literal text search; doesn't evaluate model-provided regexes."""
    try:
        root=WS.path(path)
        if not root.exists():
            return '[ERROR] 搜索路径不存在。'
        if root.is_file():
            lines=WS.read_text(str(root)).splitlines()
            hits=[f"{root.name}:{number}: {line[:240]}" for number,line in enumerate(lines,1) if pattern in line]
            return '匹配结果：\n'+'\n'.join(hits[:50]) if hits else '没有找到匹配文本。'
        results=[];visited=0
        for base,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[name for name in dirs if _visible_path(str(Path(base)/name))]
            for name in files:
                visited+=1
                if visited>3000:
                    return '匹配结果：\n'+'\n'.join(results)+ '\n[已达到 3000 个文件的扫描限制]'
                file=Path(base)/name
                if glob and not _matches_glob(name,file.relative_to(root).as_posix(),glob):
                    continue
                try:
                    if file.stat().st_size>1024*1024:
                        continue
                    text=WS.read_text(str(file))
                except (OSError,ValueError,PermissionError):
                    continue
                for number,line in enumerate(text.splitlines(),1):
                    if pattern in line:
                        results.append(f"{file.relative_to(WS.root)}:{number}: {line.strip()[:240]}")
                        if len(results)>=50:
                            return '匹配结果：\n'+'\n'.join(results)+'\n[已显示前 50 处匹配]'
                if visited%30==0:
                    await asyncio.sleep(0)
        return '匹配结果：\n'+'\n'.join(results) if results else '没有找到匹配文本。'
    except (OSError,ValueError,PermissionError) as exc:
        return f"[ERROR] {exc}"


def _visible_path(path):
    try:
        WS.path(path)
        return True
    except (ValueError,PermissionError,OSError):
        return False


async def _list_files(path: str = "", glob: str = "**/*", max_results: int = 100) -> str:
    try:
        root=WS.path(path);results=[];visited=0
        if not root.is_dir():
            return '[ERROR] 文件查找路径不是目录。'
        for base,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[name for name in dirs if _visible_path(str(Path(base)/name))]
            for name in sorted(dirs+files):
                visited+=1
                file=Path(base)/name
                if not _visible_path(str(file)):
                    continue
                rel=file.relative_to(root).as_posix()
                if glob!='**/*' and not _matches_glob(name,rel,glob):
                    continue
                results.append(rel+('/' if file.is_dir() else ''))
                if len(results)>=min(max_results,200):
                    return '文件：\n'+'\n'.join(results)+'\n[结果已截断]'
                if visited>=3000:
                    return '文件：\n'+'\n'.join(results)+'\n[已达到扫描限制]'
            await asyncio.sleep(0)
        return '文件：\n'+'\n'.join(results) if results else '工作区里还没有匹配的文件。'
    except (OSError,ValueError,PermissionError) as exc:
        return f"[ERROR] {exc}"


async def _git_status(**kwargs) -> str:
    """Run git status in workspace."""
    return await _run_git(["status", "--short", "--ignore-submodules=all"])


async def _git_diff(staged: bool = False, **kwargs) -> str:
    """Run git diff in workspace."""
    args = ["diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all"]
    if staged:
        args.append("--staged")
    return await _run_git(args)


async def _git_log(max_count: int = 10, **kwargs) -> str:
    """Run git log in workspace."""
    return await _run_git([
        "log", "--no-show-signature", "--no-notes", f"--max-count={min(max_count, 50)}",
        "--oneline", "--decorate",
    ])


async def _git_commit(message: str, **kwargs) -> str:
    return '[BLOCKED] 提交工具待逐文件确认流程完善。'


async def _run_git(args: list[str]) -> str:
    """Helper: execute a git command in /opt/agent/workspace."""
    ws = str(WS.root)
    git_dir=WS.root/'.git'
    if not git_dir.is_dir() or git_dir.is_symlink():
        return '[ERROR] 工作区根目录不是独立的 Git 仓库。'
    env={'PATH':os.defpath,'LANG':'C.UTF-8','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,
         'GIT_TERMINAL_PROMPT':'0','GIT_NO_LAZY_FETCH':'1'}
    common=['git','--no-optional-locks','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
            '-c','core.pager=cat','-c','core.quotePath=false','-c','log.showSignature=false',
            '-c','diff.submodule=short']
    try:
        probe=await asyncio.create_subprocess_exec(*common,'config','--local','--no-includes','--name-only',
            '--get-regexp',r'^(filter\.|include\.|includeif\.|extensions\.partialclone|remote\..*\.promisor)',
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,cwd=ws,env=env,start_new_session=True)
        configured,_=await _communicate_or_stop(probe,timeout=5)
        if configured:
            return '[BLOCKED] 该仓库使用外部过滤器、额外配置或按需获取对象，暂不通过工具执行 Git。'
        if probe.returncode not in (0,1):
            return '[ERROR] 无法读取工作区的 Git 配置。'
        proc=await asyncio.create_subprocess_exec(*common,*args,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
            cwd=ws,env=env,start_new_session=True)
        stdout, stderr = await _communicate_or_stop(proc, timeout=30)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err}")
        if not parts:
            parts.append("(no output)")
        if proc.returncode != 0:
            parts.insert(0, f"[ERROR] Git exited with code {proc.returncode}.")
        return "\n".join(parts)
    except asyncio.TimeoutError:
        return "[TIMEOUT] Git command exceeded 30 seconds — stopped."
    except FileNotFoundError:
        return "[ERROR] Git not found on server"
    except Exception as exc:
        return f"[ERROR] {exc}"


async def _create_pptx(**kwargs) -> str:
    return '[BLOCKED] 演示文稿工具待输出验收。'


async def _create_pdf(**kwargs) -> str:
    return '[BLOCKED] PDF 工具待中文输出验收。'


async def _web_fetch(url: str, max_chars: int = 8000) -> str:
    from network import PublicTransport
    import httpx
    try:
        async with asyncio.timeout(20):
            async with httpx.AsyncClient(transport=PublicTransport(),trust_env=False,
                    timeout=10,follow_redirects=True,max_redirects=3) as client:
                async with client.stream('GET',url,headers={'User-Agent':'AgentPhone/0.2','Accept':'text/html,text/plain,application/json'}) as response:
                    response.raise_for_status()
                    kind=response.headers.get('content-type','').lower()
                    if kind and not any(value in kind for value in ('text/','json','xml')):
                        return '[ERROR] 这个地址返回的不是网页或文本。'
                    chunks=[];size=0
                    async for part in response.aiter_bytes():
                        size+=len(part)
                        if size>1024*1024:
                            return '[ERROR] 网页超过 1 MB 读取限制。'
                        chunks.append(part)
                    html=b''.join(chunks).decode(response.encoding or 'utf-8',errors='replace')
        from bs4 import BeautifulSoup
        soup=BeautifulSoup(html,'html.parser')
        for element in soup(['script','style','nav','footer','header']):
            element.decompose()
        text=soup.get_text('\n',strip=True)
        limit=min(max_chars,20000)
        return '[网页内容，来源需自行判断]\n'+(text[:limit] or '(无文本内容)')+('\n[内容已截断]' if len(text)>limit else '')
    except PermissionError as exc:
        return f'[BLOCKED] {exc}'
    except (TimeoutError,httpx.TimeoutException):
        return '[TIMEOUT] 网页读取超时。'
    except httpx.HTTPStatusError as exc:
        return f'[ERROR] 网页返回 HTTP {exc.response.status_code}。'
    except Exception:
        logger.exception('Web fetch failed')
        return '[ERROR] 网页读取失败。'


async def _code_exec(code: str) -> str:
    return "[BLOCKED] 尚未配置独立的代码执行环境。可以先保存代码文件。"


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


def _argument_error(value: Any, schema: dict, label: str = "arguments") -> str | None:
    """Validate the JSON types used by the current tool contracts."""
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }
    if kind not in valid or not valid[kind]:
        return f"{label} must be {kind}."
    if kind == "integer" and (value < schema.get("minimum", value) or value > schema.get("maximum", value)):
        return f"{label} is outside the allowed range."
    if "enum" in schema and value not in schema["enum"]:
        return f"{label} is not an allowed value."
    if kind == "object":
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return f"{label} is missing required fields."
        if any(key not in properties for key in value):
            return f"{label} contains unknown fields."
        for key, child in value.items():
            error = _argument_error(child, properties[key], f"{label}.{key}")
            if error:
                return error
    elif kind == "array" and "items" in schema:
        for index, child in enumerate(value):
            error = _argument_error(child, schema["items"], f"{label}[{index}]")
            if error:
                return error
    return None


_TOOL_SCHEMAS = {item["function"]["name"]: item["function"]["parameters"] for item in TOOL_DEFINITIONS}
_DISABLED = {'code_exec': '代码运行环境尚未配置', 'git_commit': '提交工具待逐文件确认流程完善',
             'create_pdf': 'PDF 工具待中文输出验收', 'create_pptx': '演示文稿工具待输出验收'}
_DEPENDENCIES = {'web_search':['ddgs'],'web_fetch':['httpx','bs4']}
_LOCKS = weakref.WeakKeyDictionary()


def write_lock():
    loop=asyncio.get_running_loop()
    if loop not in _LOCKS:
        _LOCKS[loop]=asyncio.Lock()
    return _LOCKS[loop]


def capability_status():
    result=[]
    for item in TOOL_DEFINITIONS:
        name=item['function']['name'];reason=_DISABLED.get(name,'')
        dependency=_DEPENDENCIES.get(name)
        if dependency and any(importlib.util.find_spec(module) is None for module in dependency):
            reason='所需组件尚未安装'
        result.append({'name':name,'enabled':not bool(reason),'reason':reason})
    return result


def available_definitions():
    enabled={item['name'] for item in capability_status() if item['enabled']}
    return [item for item in TOOL_DEFINITIONS if item['function']['name'] in enabled]


async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    if not isinstance(name,str):
        return '[ERROR] 工具名称格式不正确。'
    executor=EXECUTORS.get(name);schema=_TOOL_SCHEMAS.get(name)
    if executor is None or schema is None:
        return f"[ERROR] Unknown tool: {name}"
    error=_argument_error(arguments,schema)
    if error:
        return f"[ERROR] Invalid arguments for {name}: {error}"
    if name in _DISABLED:
        return f"[BLOCKED] {_DISABLED[name]}。"
    try:
        if name in ('write_file','edit_file','git_commit'):
            async with write_lock():
                with observe_write(WS,name,arguments.get('path','')):
                    result=await executor(**arguments)
        else:
            result=await executor(**arguments)
        return result if isinstance(result,str) else json.dumps(result,ensure_ascii=False)
    except Exception:
        logger.exception('Tool execution failed: %s',name)
        return f"[ERROR] Tool execution failed: {name}."

for _item in TOOL_DEFINITIONS:
    _function=_item['function']
    if _function['name']=='read_file':
        _function['parameters']['properties']['start_line']={'type':'integer','minimum':1,'maximum':200000,'description':'起始行号，从 1 开始，可分段读取大文件。'}
    if _function['name']=='write_file':
        _function['parameters']['properties']['overwrite']={'type':'boolean','description':'默认 false。只有确实要完整替换已有文件时设置 true；局部修改优先使用 edit_file。'}
    if _function['name']=='run_command':
        _function['description']='在工作区内查看文件。支持 ls、cat、head、tail、wc、pwd、echo；不支持解释器、安装依赖或 shell 语法。修改文件请用专用工具。'
    if _function['name']=='search_code':
        _function['description']='在工作区内按字面文本查找代码，返回文件名、行号和片段。pattern 不解释为正则表达式。'
    for _key,_bounds in {'max_lines':(1,500),'max_results':(1,200),'max_count':(1,50),'max_chars':(1,20000)}.items():
        _property=_function['parameters'].get('properties',{}).get(_key)
        if _property:
            _property['minimum'],_property['maximum']=_bounds
