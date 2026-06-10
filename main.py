"""
FastAPI backend for the Agent Phone Assistant.

Serves the static chat UI and exposes:
  POST /api/chat/send         SSE streaming chat (with tool-calling loop)
  GET  /api/conversations      list saved conversations
  POST /api/conversations      create new conversation
  GET  /api/conversations/{id} get conversation messages
  DELETE /api/conversations/{id} delete conversation
  GET  /api/health             health check

The agent loop: user message → DeepSeek API (with tools) → execute any
tool_calls → feed results back → repeat (max 5 rounds) → stream final answer.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from tools import EXECUTORS, TOOL_DEFINITIONS, execute_tool, _in_allowed, ALLOWED_READ_PATHS, MAX_READ_BYTES

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("agent-phone")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "5"))
MAX_INPUT_LENGTH = int(os.getenv("MAX_INPUT_LENGTH", "4096"))
MAX_CONVERSATIONS = int(os.getenv("MAX_CONVERSATIONS", "100"))
MAX_MESSAGES_PER_CONV = int(os.getenv("MAX_MESSAGES_PER_CONV", "500"))

# Optional pre-shared token for authentication (MVP — single-user).
# If set, clients must include `Authorization: Bearer <token>`.
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "").strip()

# CORS origins — restrict in production, allow all in dev.
CORS_ORIGINS_RAW = os.getenv("CORS_ORIGINS", "")
CORS_ORIGINS = (
    [o.strip() for o in CORS_ORIGINS_RAW.split(",") if o.strip()]
    if CORS_ORIGINS_RAW
    else ["*"]
)

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "你是一个运行在 Linux 服务器上的 AI 助手，类似 Claude Code。"
    "核心能力："
    "1) 精确编辑文件（edit_file）— 替换字符串并显示 diff"
    "2) 搜索代码（search_code）— 正则 grep 搜索"
    "3) 列出文件（list_files）— glob 模式匹配"
    "4) Git 操作（git_status/diff/log/commit）— 版本控制"
    "5) 执行命令（run_command）— 运行脚本、安装依赖"
    "6) 读写文件（read_file/write_file）— 文件管理"
    "7) 网页搜索（web_search）— 查文档"
    "工作目录：/opt/agent/workspace/（你的主工作区）。"
    "回答简洁有用。使用工具时先说明意图。用中文回复。",
)

if not DEEPSEEK_API_KEY:
    raise RuntimeError("DEEPSEEK_API_KEY not set — create a .env file or export it.")

# ---------------------------------------------------------------------------
# OpenAI client (DeepSeek-compatible)
# ---------------------------------------------------------------------------

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def _verify_auth(request: Request) -> None:
    """If AUTH_TOKEN is configured, require a matching Bearer token."""
    if not AUTH_TOKEN:
        return  # auth disabled — single-user dev mode

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth_header[7:]
    if token != AUTH_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Agent Phone Assistant", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ---------------------------------------------------------------------------
# In-memory conversation store  (MVP — lost on restart)
# ---------------------------------------------------------------------------

conversations: dict[str, dict[str, Any]] = {}

def _new_conversation(title: str = "") -> dict[str, Any]:
    # Evict oldest if at capacity
    if len(conversations) >= MAX_CONVERSATIONS:
        oldest = min(
            conversations.items(),
            key=lambda kv: kv[1].get("updated_at", ""),
        )
        del conversations[oldest[0]]

    conv = {
        "id": uuid.uuid4().hex,
        "title": title or "新对话",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    conversations[conv["id"]] = conv
    return conv


def _enforce_message_limit(conv: dict[str, Any]) -> None:
    """Trim oldest non-system messages if over the limit."""
    messages = conv["messages"]
    cutoff = MAX_MESSAGES_PER_CONV + 1  # +1 for system prompt
    if len(messages) > cutoff:
        system = [m for m in messages if m["role"] == "system"]
        rest = [m for m in messages if m["role"] != "system"]
        excess = len(rest) - MAX_MESSAGES_PER_CONV
        conv["messages"] = system + rest[excess:]


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = static_dir / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=404)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def _agent_loop(messages: list[dict[str, Any]]):
    """
    Run the agent loop: call DeepSeek with tool definitions, execute any
    tool calls, feed results back, repeat.  Yields SSE event strings.

    Works on a shallow copy to avoid corrupting the conversation on abort.
    """
    # Shallow copy: new list of new dicts for existing entries.
    # Inner values (strings, lists) are shared but only appended to, never mutated.
    local_msgs = [dict(m) for m in messages]
    rounds = 0

    while rounds < MAX_TOOL_ROUNDS:
        rounds += 1

        stream = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=local_msgs,
            tools=TOOL_DEFINITIONS,
            stream=True,
            temperature=0.7,
        )

        # --- accumulate streaming response ---
        content_chunks: list[str] = []
        tool_call_acc: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None

        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            finish_reason = chunk.choices[0].finish_reason if chunk.choices else None

            if delta and delta.content:
                content_chunks.append(delta.content)
                yield f"event: delta\ndata: {json.dumps({'content': delta.content})}\n\n"

            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_call_acc:
                        tool_call_acc[idx] = {
                            "id": tc.id or "",
                            "function": {"name": "", "arguments": ""},
                        }
                    entry = tool_call_acc[idx]
                    if tc.id:
                        entry["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            entry["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            entry["function"]["arguments"] += tc.function.arguments

        # --- build assistant message ---
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        full_content = "".join(content_chunks)

        if tool_call_acc and finish_reason == "tool_calls":
            tool_calls_out: list[dict[str, Any]] = []
            for idx in sorted(tool_call_acc.keys()):
                tc = tool_call_acc[idx]
                tool_calls_out.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                })
            assistant_msg["content"] = full_content or None
            assistant_msg["tool_calls"] = tool_calls_out
            local_msgs.append(assistant_msg)

            # --- execute tools (each gets a unique call_id for frontend matching) ---
            for i, tc in enumerate(tool_calls_out):
                tool_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                call_id = f"{tc['id']}_{i}"

                yield (
                    f"event: tool_call\n"
                    f"data: {json.dumps({'call_id': call_id, 'tool_name': tool_name, 'arguments': args})}\n\n"
                )

                result = await execute_tool(tool_name, args)

                yield (
                    f"event: tool_result\n"
                    f"data: {json.dumps({'call_id': call_id, 'tool_name': tool_name, 'result': result})}\n\n"
                )

                local_msgs.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

            continue  # next round — DeepSeek gets tool results

        # --- final answer (no tool calls) ---
        if full_content:
            assistant_msg["content"] = full_content
        else:
            assistant_msg["content"] = "（空响应）"
        local_msgs.append(assistant_msg)
        break

    else:
        # max rounds hit — emit the message so the UI shows it
        exhaustion_msg = "（已达到最大工具调用轮次，已停止。）"
        yield f"event: delta\ndata: {json.dumps({'content': exhaustion_msg})}\n\n"
        local_msgs.append({
            "role": "assistant",
            "content": exhaustion_msg,
        })

    # Commit back to the original messages list
    messages.clear()
    messages.extend(local_msgs)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/api/chat/send")
async def chat_send(request: Request, _auth=Depends(_verify_auth)):
    """
    SSE streaming chat endpoint.

    Request body (JSON):
      { "conversation_id"?: string, "message": string }

    Returns text/event-stream with events: delta, tool_call, tool_result, error, done
    """
    body = await request.json()
    user_message = (body.get("message") or "").strip()
    conv_id = body.get("conversation_id")

    if not user_message:
        raise HTTPException(status_code=400, detail="message is required")
    if len(user_message) > MAX_INPUT_LENGTH:
        raise HTTPException(status_code=413, detail=f"Message too long (max {MAX_INPUT_LENGTH} chars)")

    # load or create conversation
    if conv_id and conv_id in conversations:
        conv = conversations[conv_id]
    else:
        title = user_message[:60].replace("\n", " ")
        conv = _new_conversation(title)
        conv_id = conv["id"]

    # append user message
    conv["messages"].append({"role": "user", "content": user_message})
    conv["updated_at"] = datetime.now(timezone.utc).isoformat()
    _enforce_message_limit(conv)

    async def stream():
        try:
            async for sse_event in _agent_loop(conv["messages"]):
                yield sse_event
            yield f"event: done\ndata: {json.dumps({'conversation_id': conv_id})}\n\n"
        except Exception:
            logger.exception("Agent loop failed for conversation %s", conv_id)
            yield (
                "event: error\n"
                f"data: {json.dumps({'message': '服务器内部错误，请重试。'})}\n\n"
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/conversations")
async def list_conversations(_auth=Depends(_verify_auth)):
    """Return all conversations (most recent first)."""
    result = []
    for c in conversations.values():
        result.append({
            "id": c["id"],
            "title": c["title"],
            "created_at": c["created_at"],
            "updated_at": c["updated_at"],
            "message_count": len([m for m in c["messages"] if m["role"] != "system"]),
        })
    result.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"conversations": result}


@app.post("/api/conversations")
async def create_conversation(request: Request, _auth=Depends(_verify_auth)):
    """Create a new empty conversation."""
    body = {}
    if await request.body():
        body = await request.json()
    title = (body.get("title") or "").strip() or "新对话"
    conv = _new_conversation(title)
    return {"id": conv["id"], "title": conv["title"]}


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str, _auth=Depends(_verify_auth)):
    """Return a conversation with all messages."""
    conv = conversations.get(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    display_msgs = [m for m in conv["messages"] if m["role"] != "system"]
    return {
        "id": conv["id"],
        "title": conv["title"],
        "messages": display_msgs,
        "created_at": conv["created_at"],
        "updated_at": conv["updated_at"],
    }


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str, _auth=Depends(_verify_auth)):
    """Delete a conversation."""
    if conv_id not in conversations:
        raise HTTPException(status_code=404, detail="Conversation not found")
    del conversations[conv_id]
    return {"ok": True}


@app.get("/api/files")
async def browse_files(path: str = "", _auth=Depends(_verify_auth)):
    """
    Browse the file workspace. Defaults to /opt/agent/workspace/.

    Query params:
      path  — relative path from workspace root, or absolute within allowed paths.

    Returns JSON:
      { type: "dir"|"file", name, path, entries?[], content? }
    """
    # Resolve the path
    if not path:
        target = "/opt/agent/workspace"
    elif path.startswith("/"):
        target = path
    else:
        target = f"/opt/agent/workspace/{path}"

    try:
        resolved = _in_allowed(target, ALLOWED_READ_PATHS)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Path not allowed")

    import stat as stat_module

    try:
        st = resolved.stat()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")

    # Directory listing
    if resolved.is_dir():
        entries = []
        try:
            for entry in sorted(resolved.iterdir()):
                try:
                    es = entry.stat()
                    entries.append({
                        "name": entry.name,
                        "is_dir": entry.is_dir(),
                        "size": es.st_size if not entry.is_dir() else 0,
                        "mtime": int(es.st_mtime),
                    })
                except OSError:
                    pass
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")

        return {
            "type": "dir",
            "name": resolved.name or resolved.as_posix(),
            "path": str(resolved),
            "entries": entries,
        }

    # File read
    if not stat_module.S_ISREG(st.st_mode):
        raise HTTPException(status_code=400, detail="Not a regular file")

    if st.st_size > MAX_READ_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (>{MAX_READ_BYTES} bytes)")

    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except Exception:
        raise HTTPException(status_code=400, detail="Cannot read as text")

    return {
        "type": "file",
        "name": resolved.name,
        "path": str(resolved),
        "size": st.st_size,
        "content": content,
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "model": DEEPSEEK_MODEL,
        "tool_count": len(EXECUTORS),
        "conversations": len(conversations),
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
