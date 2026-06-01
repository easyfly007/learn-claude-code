#!/usr/bin/env python3
# Harness: context isolation -- protecting the model's clarity of thought.
"""
s04_subagent.py - subagent

spawn a child agent with fresh messages=[]. The child works in its own context,
sharing the filesystem, then returns only a summary to the parent.

    Parent agent                    Sub agent
    +-------------------+           +-------------------+
    | messages = [...]  |           | messages = []     | <- fresh
    |                   | dispatch  |                   |
    | tool: task        | --------> | while tool_use:   |
    |    prompt='...'   |           |   call tools      |
    |    description=...|           |   append results  |
    |                   | summary   |                   |
    |   result = '...'  | <-------- | return last text  |
    +-------------------+           +-------------------+
               |
    Parent context stays clean
    subagent context is discarded

Key insight: "Fresh messages=[] gives context isolation. The parent stays clean."

Note: Real claude code also uses in-process isolation (not os-level process
forking). the child runs in the same process with a fresh message array and
isolated tool context -- same pattern as this teaching implementation


    comparison with real claude code:
    ================================================================================================
    | Aspect                    | this demo                 | real claude code                      |
    +---------------------------+---------------------------+---------------------------------------+
    | Backend                   | in-process only           | 5 backends:                           |
    |                           |                           |       in-process,                     |
    |                           |                           |       tmux,                           |
    |                           |                           |       iterm2,                         |
    |                           |                           |       fork,                           |
    |                           |                           |       remote                          |
    |                           |                           |                                       |
    | context isolation         | fresh messages=[]         | create subagentContext() isolates     |
    |                           |                           | ~20 fields                            |
    |                           |                           |       tools, permissions,             |
    |                           |                           |       cwd, env, hooks, etc            |
    |                           |                           |                                       |
    | tool filtering            | manually curated          | resolveAgentTools() filters from      |
    |                           |                           | parent pool;                          |
    |                           |                           | allowedTools replace all allow tools  |
    |                           |                           |                                       |
    | agent definition          | hardcoded system prompt   | .claude/agent/*.md with YAML          |
    |                           |                           |       frontmatter (AgentTemplate)     |
    +---------------------------+---------------------------+---------------------------------------+

PROVIDER env var picks the backend:
    PROVIDER=anthropic   -> Anthropic SDK (claude-*)
    PROVIDER=deepseek    -> OpenAI SDK pointing at DeepSeek (deepseek-chat)
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# 防止 locale 不是 UTF-8 时 input() 把汉字字节解成孤立代理，
# 导致后续 httpx strict-UTF-8 编码请求体时报 'surrogates not allowed'
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def _sanitize(s: str) -> str:
    return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


PROVIDER = os.environ.get("PROVIDER", "anthropic").lower()
MODEL = os.environ["MODEL_ID"]
WORKDIR = Path.cwd()
SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."


def _init_client():
    if PROVIDER == "anthropic":
        from anthropic import Anthropic
        # 空串会被 SDK 当 URL 传给 httpx 报错，主动清掉
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


class AgentTemplate:
    """
    parse agent definition from markdown frontmatter
    real claude code loads agent definitions from .claude/agents/*.md
    frontmatter fields:
        name, tools, disallowedTools, skills, hooks,
        model, effort, permissionMode, maxTurns, memory, isolation, color,
        background, initialPrompt, mcpServers
    3 sources: built-in, custom, plugin-provided
    """

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        self.config = {}
        self.system_prompt = ""
        self._parse()

    def _parse(self):
        text = self.path.read_text()
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            self.system_prompt = text
            return
        for line in match.group(1).splitlines():
            if ":" in line:
                k,_,v = line.partition(":")
                self.config[k.strip()] = v.strip()
        self.system_prompt = match.group(2).strip()
        self.name = self.config.get("name", self.name)

# -- Tool implementations shared by parent and child --
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
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
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

TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}



# Child gets all base tools except task (no recursive spawning)
CHILD_TOOLS_ANTHROPIC = [
        {
            "name": "bash",
            "description": "Run a shell command.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"}
                    },
                "required": ["command"]
                }
            },
        {
            "name": "read_file",
            "description": "Read file contents.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer"}
                    },
                "required": ["path"]
                }
            },
        {
            "name": "write_file",
            "description": "Write content to file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                    },
                "required": ["path", "content"]
                }
            },
        {
            "name": "edit_file",
            "description": "Replace exact text in file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"}
                    },
                "required": ["path", "old_text", "new_text"]
                }
            },
        ]

PARENT_TOOLS_ANTHROPIC = CHILD_TOOLS_ANTHROPIC + [
        {
            "name": "task",
            "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "description": {"type": "string", "description": "Short description of the task"
                        }
                    },
                "required": ["prompt"]
                }
            },
        ]


def _to_openai_tools(tools):
    return [
        {"type": "function",
         "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
        for t in tools
    ]


CHILD_TOOLS_OPENAI = _to_openai_tools(CHILD_TOOLS_ANTHROPIC)
PARENT_TOOLS_OPENAI = _to_openai_tools(PARENT_TOOLS_ANTHROPIC)


# ============================================================
# Subagent: Anthropic 分支
# ============================================================
def _subagent_anthropic(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]
    response = None
    for _ in range(30):
        response = client.messages.create(
                model=MODEL,
                system=SUBAGENT_SYSTEM,
                messages=sub_messages,
                tools=CHILD_TOOLS_ANTHROPIC,
                max_tokens=8000)
        sub_messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as exc:
                    output = f"Error: {exc}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
        sub_messages.append({"role": "user", "content": results})
    if response is None:
        return "(no summary)"
    return "".join(b.text for b in response.content if getattr(b, "type", None) == "text") or "(no summary)"


# ============================================================
# Subagent: DeepSeek (OpenAI 兼容) 分支
# ============================================================
def _subagent_deepseek(prompt: str) -> str:
    sub_messages = [
        {"role": "system", "content": SUBAGENT_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    last_text = ""
    for _ in range(30):
        response = client.chat.completions.create(
            model=MODEL,
            messages=sub_messages,
            tools=CHILD_TOOLS_OPENAI,
            max_tokens=8000,
        )
        msg = response.choices[0].message
        finish = response.choices[0].finish_reason
        if msg.content:
            last_text = msg.content
        assistant_msg = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        sub_messages.append(assistant_msg)
        if finish != "tool_calls" or not msg.tool_calls:
            break
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                args, output = {}, f"Error: bad JSON args: {exc}"
            else:
                handler = TOOL_HANDLERS.get(tc.function.name)
                try:
                    output = handler(**args) if handler else f"Unknown tool: {tc.function.name}"
                except Exception as exc:
                    output = f"Error: {exc}"
            sub_messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": str(output)[:50000],
            })
    return last_text or "(no summary)"


def run_subagent(prompt: str) -> str:
    if PROVIDER == "anthropic":
        return _subagent_anthropic(prompt)
    return _subagent_deepseek(prompt)


# ============================================================
# Parent loop: Anthropic 分支
# ============================================================
def _parent_turn_anthropic(messages: list) -> bool:
    response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=PARENT_TOOLS_ANTHROPIC,
            max_tokens=8000)
    messages.append({"role": "assistant", "content": response.content})
    if response.stop_reason != "tool_use":
        return False
    results = []
    for block in response.content:
        if block.type != "tool_use":
            continue
        if block.name == "task":
            desc = block.input.get("description", "subtask")
            prompt = block.input.get("prompt", "")
            print(f"> task({desc}): {prompt[:80]}")
            output = run_subagent(prompt)
        else:
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
            except Exception as exc:
                output = f"Error: {exc}"
        print(f"  {str(output)[:200]}")
        results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
    messages.append({"role": "user", "content": results})
    return True


# ============================================================
# Parent loop: DeepSeek (OpenAI 兼容) 分支
# ============================================================
def _parent_turn_deepseek(messages: list) -> bool:
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=PARENT_TOOLS_OPENAI,
        max_tokens=8000,
    )
    msg = response.choices[0].message
    finish = response.choices[0].finish_reason
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
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError as exc:
            args, output = {}, f"Error: bad JSON args: {exc}"
        else:
            if tc.function.name == "task":
                desc = args.get("description", "subtask")
                prompt = args.get("prompt", "")
                print(f"> task({desc}): {prompt[:80]}")
                output = run_subagent(prompt)
            else:
                handler = TOOL_HANDLERS.get(tc.function.name)
                try:
                    output = handler(**args) if handler else f"Unknown tool: {tc.function.name}"
                except Exception as exc:
                    output = f"Error: {exc}"
        print(f"  {str(output)[:200]}")
        messages.append({
            "role": "tool", "tool_call_id": tc.id,
            "content": str(output)[:50000],
        })
    return True


def agent_loop(messages: list, max_turns: int = 25):
    turn = _parent_turn_anthropic if PROVIDER == "anthropic" else _parent_turn_deepseek
    for _ in range(max_turns):
        if not turn(messages):
            return
    print(f"[warn] reached max_turns ({max_turns}), stopping loop")


if __name__ == "__main__":
    print(f"[provider={PROVIDER}, model={MODEL}]")
    # OpenAI 协议把 system 放进 messages；Anthropic 是单独的 kwarg
    history = [{"role": "system", "content": SYSTEM}] if PROVIDER == "deepseek" else []
    while True:
        try:
            query = _sanitize(input("\033[36ms04 >> \033[0m"))
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # Anthropic 最终 assistant 消息 content 是 block 列表，需要解出来打印；
        # DeepSeek 在每轮 turn 里已经 print 过 msg.content 了
        if PROVIDER == "anthropic":
            response_content = history[-1].get("content", [])
            if isinstance(response_content, list):
                for block in response_content:
                    text = getattr(block, "text", None)
                    if text:
                        print(text)
        print()
