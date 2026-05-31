#!/usr/bin/env python3
# Harness: tool dispatch -- expanding what the model can reach.
"""
s02_tool_use.py - Tool dispatch + message normalization
The agent loop from s01 didn't change. We added tools to the dispatch map,
and a normalize_messages() function that cleans up the message list before
each API call.
Key insight: "The loop didn't change at all. I just added tools."

PROVIDER env var picks the backend:
    PROVIDER=anthropic   -> Anthropic SDK (claude-*)
    PROVIDER=deepseek    -> OpenAI SDK pointing at DeepSeek (deepseek-chat)
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(override=True)

# 防止 locale 不是 UTF-8 时 input() 把汉字字节解成孤立代理 (lone surrogates)，
# 导致后续 httpx strict-UTF-8 编码请求体时报 'surrogates not allowed'
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def _sanitize(s: str) -> str:
    """兜底：把字符串里残留的孤立代理还原成原始字节再用 UTF-8 重新解码，无效序列替换掉。"""
    return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")

PROVIDER = os.environ.get("PROVIDER", "anthropic").lower()
MODEL = os.environ["MODEL_ID"]
WORKDIR = Path.cwd()
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


def _init_client():
    if PROVIDER == "anthropic":
        from anthropic import Anthropic
        # 空串会被 SDK 当作 "用户传了 URL" 传给 httpx 报错，要主动清掉
        if not os.environ.get("ANTHROPIC_BASE_URL"):
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        if not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        return Anthropic()
    if PROVIDER == "deepseek":
        from openai import OpenAI
        return OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    raise ValueError(f"Unknown PROVIDER: {PROVIDER!r}, expected 'anthropic' or 'deepseek'")


client = _init_client()
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
def run_read(path: str, limit: int = None) -> str:
    try:
        text = safe_path(path).read_text()
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"
def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"
def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"
# -- Concurrency safety classification --
# Read-only tools can safely run in parallel; mutating tools must be serialized.
CONCURRENCY_SAFE = {"read_file"}
CONCURRENCY_UNSAFE = {"write_file", "edit_file"}
# -- The dispatch map: {tool_name: handler} --
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}
TOOLS_ANTHROPIC = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]
# OpenAI / DeepSeek format wraps each tool under function.parameters
TOOLS_OPENAI = [
    {"type": "function",
     "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOLS_ANTHROPIC
]
def normalize_messages(messages: list) -> list:
    """Clean up messages before sending to the API.
    Three jobs:
    1. Strip internal metadata fields the API doesn't understand
    2. Ensure every tool_use has a matching tool_result (insert placeholder if missing)
    3. Merge consecutive same-role messages (API requires strict alternation)
    """
    cleaned = []
    for msg in messages:
        clean = {"role": msg["role"]}
        if isinstance(msg.get("content"), str):
            clean["content"] = msg["content"]
        elif isinstance(msg.get("content"), list):
            clean["content"] = [
                {k: v for k, v in block.items()
                 if not k.startswith("_")}
                for block in msg["content"]
                if isinstance(block, dict)
            ]
        else:
            clean["content"] = msg.get("content", "")
        cleaned.append(clean)
    # Collect existing tool_result IDs
    existing_results = set()
    for msg in cleaned:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    existing_results.add(block.get("tool_use_id"))
    # Find orphaned tool_use blocks and insert placeholder results
    for msg in cleaned:
        if msg["role"] != "assistant" or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id") not in existing_results:
                cleaned.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": block["id"],
                     "content": "(cancelled)"}
                ]})
    # Merge consecutive same-role messages
    if not cleaned:
        return cleaned
    merged = [cleaned[0]]
    for msg in cleaned[1:]:
        if msg["role"] == merged[-1]["role"]:
            prev = merged[-1]
            prev_c = prev["content"] if isinstance(prev["content"], list) \
                else [{"type": "text", "text": str(prev["content"])}]
            curr_c = msg["content"] if isinstance(msg["content"], list) \
                else [{"type": "text", "text": str(msg["content"])}]
            prev["content"] = prev_c + curr_c
        else:
            merged.append(msg)
    return merged
def _dbg_api(direction: str, payload) -> None:
    """LLM API I/O 调试日志：首行 [debug] api <direction>: <json>，内容中间无空行，末尾留一个空行。"""
    try:
        rendered = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(payload)
    print(f"[debug] api {direction}: {rendered}")
    print()
def _fmt_call(name: str, args: dict, max_arg_len: int = 200) -> str:
    """工具调用展示：tool_name(arg1='...', arg2=...)，长字符串截断。"""
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > max_arg_len:
            shown = v[:max_arg_len] + f"...<+{len(v) - max_arg_len} chars>"
            rendered = repr(shown)
        else:
            rendered = repr(v)
        parts.append(f"{k}={rendered}")
    return f"{name}({', '.join(parts)})"
def _anthropic_turn(messages: list) -> bool:
    cleaned = normalize_messages(messages)
    _dbg_api("input", {
        "model": MODEL,
        "system": SYSTEM,
        "messages": cleaned,
        "tools": TOOLS_ANTHROPIC,
        "max_tokens": 8000,
    })
    response = client.messages.create(
        model=MODEL, system=SYSTEM,
        messages=cleaned,
        tools=TOOLS_ANTHROPIC, max_tokens=8000,
    )
    _dbg_api("output", {
        "stop_reason": response.stop_reason,
        "usage": getattr(response, "usage", None),
        "content": [b.model_dump() if hasattr(b, "model_dump") else str(b)
                    for b in response.content],
    })
    messages.append({"role": "assistant", "content": response.content})
    if response.stop_reason != "tool_use":
        return False
    results = []
    for block in response.content:
        if block.type == "tool_use":
            handler = TOOL_HANDLERS.get(block.name)
            print(f"\033[33m> {_fmt_call(block.name, dict(block.input))}\033[0m")
            output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
            print(output[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
    messages.append({"role": "user", "content": results})
    return True


def _deepseek_turn(messages: list) -> bool:
    _dbg_api("input", {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS_OPENAI,
        "max_tokens": 8000,
    })
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=TOOLS_OPENAI,
        max_tokens=8000,
    )
    msg = response.choices[0].message
    finish = response.choices[0].finish_reason
    _dbg_api("output", {
        "finish_reason": finish,
        "usage": response.usage.model_dump() if response.usage else None,
        "content": msg.content,
        "tool_calls": [
            {"id": tc.id,
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in (msg.tool_calls or [])
        ],
    })

    assistant_msg = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        assistant_msg["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls
        ]
    messages.append(assistant_msg)

    if msg.content:
        print(msg.content)

    if finish != "tool_calls" or not msg.tool_calls:
        return False

    for tc in msg.tool_calls:
        # OpenAI 协议里 arguments 是 JSON 字符串
        args = json.loads(tc.function.arguments)
        handler = TOOL_HANDLERS.get(tc.function.name)
        print(f"\033[33m> {_fmt_call(tc.function.name, args)}\033[0m")
        output = handler(**args) if handler else f"Unknown tool: {tc.function.name}"
        print(output[:200])
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": output,
        })
    return True


def agent_loop(messages: list):
    turn = _anthropic_turn if PROVIDER == "anthropic" else _deepseek_turn
    while turn(messages):
        pass
if __name__ == "__main__":
    print(f"[provider={PROVIDER}, model={MODEL}]")
    # OpenAI 协议把 system 放进 messages；Anthropic 是单独的 kwarg
    history = [{"role": "system", "content": SYSTEM}] if PROVIDER == "deepseek" else []
    while True:
        try:
            query = _sanitize(input("\033[36ms02 >> \033[0m"))
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # Anthropic 的最终 assistant 消息 content 是 block 列表，需要解出来打印；
        # DeepSeek 的 content 已经在 turn 里 print 过了。
        if PROVIDER == "anthropic":
            response_content = history[-1]["content"]
            if isinstance(response_content, list):
                for block in response_content:
                    if hasattr(block, "text"):
                        print(block.text)
        print()

