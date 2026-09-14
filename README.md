# Agent Phone Assistant

> 手机浏览器打开就能用的 LLM Agent 助手 —— 多轮对话 + 工具调用循环 + 云端工作区。

一个部署在云服务器上的轻量级 AI Agent 系统。前端是一个单文件网页，后端用 FastAPI 驱动 DeepSeek 完成「模型决策 → 调用工具 → 结果回灌 → 继续决策」的自主循环，而不是单轮问答。

---

## 特性

### 🔁 Agent 工具调用循环

模型每次可自主决定调哪个工具、传什么参数，拿到结果后继续决策，直到不再需要工具为止（默认最多 5 轮）。

实现上处理了流式响应里比较麻烦的一块：**`tool_calls` 是分片返回的** —— 需要按 `index` 归并，把 `function.name` 与 `function.arguments` 逐片拼起来，再按 index 排序还原成完整调用列表，才能回灌给下一轮。见 [`main.py`](main.py) 的 `_agent_loop()`。

### 🧰 15 个内置工具

| 分类 | 工具 |
|---|---|
| Shell | `run_command` |
| 文件 | `read_file` · `write_file` · `edit_file` · `list_files` · `search_code` |
| Git | `git_status` · `git_diff` · `git_log` · `git_commit` |
| 网络 | `web_search` · `web_fetch` |
| 文档 | `create_pptx` · `create_pdf` |
| 执行 | `code_exec`（受限沙箱） |

工具定义（名称 + 描述 + 参数 JSON Schema）见 [`tools.py`](tools.py)，由 `execute_tool()` 统一分发。

### ☁️ 云盘

`/opt/agent/workspace/` 作为个人云盘，网页端可浏览 / 新建 / 编辑 / 上传 / 删除 / 下载。

### 💾 对话持久化

SQLite 存储对话，进程重启不丢。支持多会话：列表 / 新建 / 读取 / 删除。

### 🔐 鉴权

设置 `LOGIN_PASSWORD` 后启用登录页，登录换取 Bearer token，后续请求携带 token 访问受保护接口。

---

## 架构

```
手机浏览器 (static/index.html)
    │  HTTP + SSE 流式
    ▼
Nginx (反代 :80 → :8000，注入安全响应头)
    ▼
FastAPI (main.py)
    ├── POST /api/chat/send        → _agent_loop()，SSE 推流
    ├── /api/conversations         → SQLite 会话管理
    ├── /api/files/*               → 云盘管理
    ├── /api/auth/*                → 登录 / 校验
    └── GET  /api/health           → 健康检查
            │
            ▼
      DeepSeek API (openai SDK)
            │
            ▼
      tools.py → execute_tool() → 15 个工具实现
```

---

## 快速开始

### 环境要求

- Python 3.10+
- 一个 DeepSeek API Key

### 安装

```bash
git clone https://github.com/5hlw31/agent-phone.git
cd agent-phone
pip install -r requirements.txt
```

### 配置

```bash
cp .env.example .env
# 编辑 .env，至少填写 DEEPSEEK_API_KEY
```

主要配置项（完整见 [`.env.example`](.env.example)）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `DEEPSEEK_API_KEY` | **必填** | — |
| `DEEPSEEK_BASE_URL` | API 地址 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 模型 | `deepseek-chat` |
| `LOGIN_PASSWORD` | 登录密码；**留空则关闭鉴权（仅限开发环境）** | 空 |
| `MAX_TOOL_ROUNDS` | 单次对话最多几轮工具调用 | `5` |
| `MAX_CONVERSATIONS` | 最多保留会话数 | `100` |
| `MAX_MESSAGES_PER_CONV` | 单会话最大消息数 | `500` |
| `CORS_ORIGINS` | 允许的跨域来源 | `*` |
| `SYSTEM_PROMPT` | 系统提示词 | 见 `.env.example` |

### 运行

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

浏览器打开 `http://localhost:8000`。

### 部署（Nginx 反代）

```nginx
server {
    listen 80;
    server_name your-domain.example;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;      # SSE 流式输出必须关闭缓冲
        proxy_read_timeout 300s;
    }
}
```

> 若忘记 `proxy_buffering off`，前端会一直等到整段响应结束才显示内容。

---

## 安全设计

### 已实现的机制

| 机制 | 实现方式 |
|---|---|
| 文件路径限制 | 读写类工具仅允许配置好的目录，越界一律拒绝 |
| 命令注入防护 | `shlex.split()` 拆分参数，阻止 shell 元字符注入 |
| SSRF 防护 | `web_fetch` 拦截 `localhost`、私网 IP 与云元数据地址 |
| 沙箱 | `code_exec` 中将 `os` / `subprocess` / `socket` 置为 `None` |
| XSS 防护 | 前端 `marked` 渲染后经 `DOMPurify` 净化 |
| 登录速率限制 | 连续失败 3 次后返回 429 |
| 安全响应头 | `X-Frame-Options` / `X-Content-Type-Options` / `X-XSS-Protection` / `Referrer-Policy` / `Permissions-Policy` |
| CORS | 来源白名单 |

### 渗透测试

2026-06-11 对本系统做过一轮自测（**代码审计 + 黑盒测试**），覆盖认证、SSRF、命令注入、路径穿越、沙箱逃逸、信息泄露、速率限制七个维度。

完整报告：[`pentest-report.md`](pentest-report.md)

**结果：1 CRITICAL + 4 MEDIUM + 2 LOW + 13 项 PASS**

最严重的一个是典型的 **LLM Agent 工具滥用**问题：

> `run_command` 的命令白名单里保留了 `cat` / `grep` / `find`，这些命令可**绕过 `read_file` 的路径限制**，直接读取 `/etc/passwd`，甚至 `.env` 里的 API Key。
>
> 对应 **OWASP LLM Top 10 · LLM06 Excessive Agency（过度代理）** —— 授予 Agent 的工具权限过大。

已在 commit `4de52f5` 收紧白名单（移除 `curl` / `wget`，将 `cat` / `grep` / `find` 限制到允许目录）。

> **为什么这个漏洞值得单独说**：它不是普通的 Web 漏洞，而是 LLM Agent 特有的问题 ——
> 模型自主调用工具 + 工具权限过大 = 越权，且**攻击载荷是自然语言**，传统输入校验拦不住。

---

## 项目结构

```
agent-phone/
├── main.py               FastAPI 应用：Agent 循环、鉴权、会话存储、云盘接口
├── tools.py              15 个工具的定义（JSON Schema）与实现
├── static/index.html     单文件前端（marked + DOMPurify，无构建步骤）
├── requirements.txt
├── .env.example          配置模板
├── pentest-report.md     自渗透测试报告
└── .gitignore
```

---

## 已知限制

自我评估，按重要性排序：

- **无并发处理** —— 单进程运行，多用户同时请求会排队
- **无可观测性** —— 只有基础 logging，无指标、无链路追踪
- **安全属工程级** —— 白名单与路径限制是工程缓解，不是沙箱级隔离；若要执行不可信代码应改用容器
- **单用户模型** —— 一个 token 对应所有访问者，无多租户隔离

---

## License

未指定。使用前请先联系作者。
