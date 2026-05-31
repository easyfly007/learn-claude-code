#!/usr/bin/env python3
# harness: planning -- keep the current session plan outside the model's head
"""
s03_todo_write.py - 会话级 todo planning + 多工具 dispatch

this chapter is about a lightweight session plan, not a durable task graph.
the model can rewrite its current plan, keep one active step in focus and get
nudged if it stops refreshing the plan for too many rounds.

PROVIDER env var picks the backend:
    PROVIDER=anthropic   -> Anthropic SDK (claude-*)
    PROVIDER=deepseek    -> OpenAI SDK pointing at DeepSeek (deepseek-chat)
"""
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
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
PLAN_REMINDER_INTERVAL = 3
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use the todo tool for multi-step work.
Keep exactly one step in_progress when a task has multiple steps.
Refresh the plan as work advances. Prefer tools over prose."""


def _init_client():
    if PROVIDER == "anthropic":
        from anthropic import Anthropic
        # 空串会被 SDK 当成 URL 传给 httpx 报错，要主动清掉
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


# ============================================================
# TodoManager (会话级 plan)
# ============================================================
@dataclass
class PlanItem:
    content: str
    status: str = "pending"
    active_form: str = ""


@dataclass
class PlanningState:
    items: list[PlanItem] = field(default_factory=list)
    rounds_since_update: int = 0


class TodoManager:
    def __init__(self):
        self.state = PlanningState()

    def update(self, items: list) -> str:
        if len(items) > 12:
            raise ValueError("Keep the session plan short (max 12 items)")
        normalized = []
        in_progress_count = 0
        for index, raw_item in enumerate(items):
            content = str(raw_item.get("content", "")).strip()
            status = str(raw_item.get("status", "pending")).lower()
            active_form = str(raw_item.get("activeForm", "")).strip()
            if not content:
                raise ValueError(f"Item {index}: content required")
            if status not in {"pending", "in_progress", "completed"}:
                raise ValueError(f"Item {index}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            normalized.append(PlanItem(content=content, status=status, active_form=active_form))
        if in_progress_count > 1:
            raise ValueError("Only one plan item can be in in_progress")
        self.state.items = normalized
        self.state.rounds_since_update = 0
        return self.render()

    def note_round_without_update(self) -> None:
        self.state.rounds_since_update += 1

    def reminder(self) -> str | None:
        if not self.state.items:
            return None
        if self.state.rounds_since_update < PLAN_REMINDER_INTERVAL:
            return None
        # 触发后重置计数，下次再过 PLAN_REMINDER_INTERVAL 轮才会再提醒
        self.state.rounds_since_update = 0
        return "<reminder>Refresh your current plan before continuing.</reminder>"

    def render(self) -> str:
        if not self.state.items:
            return "No session plan yet"
        lines = []
        for item in self.state.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item.status]
            line = f"{marker} {item.content}"
            if item.status == "in_progress" and item.active_form:
                line += f" ({item.active_form})"
            lines.append(line)
        completed = sum(1 for item in self.state.items if item.status == "completed")
        lines.append(f"\n({completed}/{len(self.state.items)} completed)")
        return "\n".join(lines)


TODO = TodoManager()


# ============================================================
# 工具实现：sandbox + bash + 文件读写
# ============================================================
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(item in command for item in dangerous):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as exc:
        return f"Error: {exc}"
    output = (result.stdout + result.stderr).strip()
    return output[:50000] if output else "(no output)"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as exc:
        return f"Error: {exc}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        content = file_path.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        file_path.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo": lambda **kw: TODO.update(kw["items"]),
}


# ============================================================
# 工具 schema：先写 Anthropic 版，再机械翻译成 OpenAI 版
# ============================================================
TOOLS_ANTHROPIC = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "todo", "description": "Rewrite the current session plan for multi-step work.",
     "input_schema": {"type": "object",
                      "properties": {
                          "items": {"type": "array",
                                    "items": {"type": "object",
                                              "properties": {
                                                  "content": {"type": "string"},
                                                  "status": {"type": "string",
                                                             "enum": ["pending", "in_progress", "completed"]},
                                                  "activeForm": {"type": "string",
                                                                 "description": "Optional present-continuous label."}},
                                              "required": ["content", "status"]}}},
                      "required": ["items"]}},
]

# OpenAI / DeepSeek format wraps each tool under function.parameters
TOOLS_OPENAI = [
    {"type": "function",
     "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOLS_ANTHROPIC
]


# ============================================================
# debug helpers (与 s02 一致)
# ============================================================
def _dbg_api(direction: str, payload) -> None:
    try:
        rendered = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(payload)
    print(f"[debug] api {direction}: {rendered}")
    print()


def _fmt_call(name: str, args: dict, max_arg_len: int = 200) -> str:
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > max_arg_len:
            shown = v[:max_arg_len] + f"...<+{len(v) - max_arg_len} chars>"
            rendered = repr(shown)
        else:
            rendered = repr(v)
        parts.append(f"{k}={rendered}")
    return f"{name}({', '.join(parts)})"


# ============================================================
# Anthropic 分支
# ============================================================
def _anthropic_turn(messages: list) -> bool:
    _dbg_api("input", {
        "model": MODEL, "system": SYSTEM,
        "messages": messages, "tools": TOOLS_ANTHROPIC, "max_tokens": 8000,
    })
    response = client.messages.create(
        model=MODEL, system=SYSTEM,
        messages=messages, tools=TOOLS_ANTHROPIC, max_tokens=8000,
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
    used_todo = False
    for block in response.content:
        if block.type != "tool_use":
            continue
        handler = TOOL_HANDLERS.get(block.name)
        print(f"\033[33m> {_fmt_call(block.name, dict(block.input))}\033[0m")
        try:
            output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
        except Exception as exc:
            output = f"Error: {exc}"
        print(str(output)[:200])
        results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        if block.name == "todo":
            used_todo = True

    if not used_todo:
        TODO.note_round_without_update()
        reminder = TODO.reminder()
        if reminder:
            # Anthropic user 消息可以混合 text 和 tool_result 块
            results.insert(0, {"type": "text", "text": reminder})

    messages.append({"role": "user", "content": results})
    return True


# ============================================================
# DeepSeek (OpenAI 兼容) 分支
# ============================================================
def _deepseek_turn(messages: list) -> bool:
    _dbg_api("input", {
        "model": MODEL, "messages": messages,
        "tools": TOOLS_OPENAI, "max_tokens": 8000,
    })
    response = client.chat.completions.create(
        model=MODEL, messages=messages,
        tools=TOOLS_OPENAI, max_tokens=8000,
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

    used_todo = False
    for tc in msg.tool_calls:
        # OpenAI 协议里 arguments 是 JSON 字符串
        args = json.loads(tc.function.arguments)
        handler = TOOL_HANDLERS.get(tc.function.name)
        print(f"\033[33m> {_fmt_call(tc.function.name, args)}\033[0m")
        try:
            output = handler(**args) if handler else f"Unknown tool: {tc.function.name}"
        except Exception as exc:
            output = f"Error: {exc}"
        print(str(output)[:200])
        messages.append({
            "role": "tool", "tool_call_id": tc.id, "content": str(output),
        })
        if tc.function.name == "todo":
            used_todo = True

    if not used_todo:
        TODO.note_round_without_update()
        reminder = TODO.reminder()
        if reminder:
            # OpenAI 协议不允许 user 消息里混 tool_result，单独追加一条 user 提醒
            messages.append({"role": "user", "content": reminder})

    return True


# ============================================================
# 调度 + 主循环
# ============================================================
def agent_loop(messages: list, max_turns: int = 25) -> None:
    turn = _anthropic_turn if PROVIDER == "anthropic" else _deepseek_turn
    for _ in range(max_turns):
        if not turn(messages):
            return
    print(f"[warn] reached max_turns ({max_turns}), stopping loop")


def extract_text(content) -> str:
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


if __name__ == "__main__":
    print(f"[provider={PROVIDER}, model={MODEL}]")
    # OpenAI 协议把 system 放进 messages；Anthropic 是单独的 kwarg
    history = [{"role": "system", "content": SYSTEM}] if PROVIDER == "deepseek" else []
    while True:
        try:
            query = _sanitize(input("\033[36ms03 >> \033[0m"))
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", "quit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # Anthropic 的最终 assistant 消息 content 是 block 列表，需要解出来打印；
        # DeepSeek 在每轮 turn 里已经 print 过 msg.content 了
        if PROVIDER == "anthropic":
            final_text = extract_text(history[-1]["content"])
            if final_text:
                print(final_text)
        print()
