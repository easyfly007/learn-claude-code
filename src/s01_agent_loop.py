#!/usr/bin/env python3
# harness: the loop -- keep feeding real tool results back into the model
"""
s01_agent_loop.py - the agent loop

this file teaches the smallest useful coding-agent pattern:
    user message
        -> model reply
        -> if tool_use: execute tools
        -> write tool_result back to message
        -> continue

it intentionally keeps the loop small, but still makes the loop state explicit
so later chapters can grow from the same structure.

PROVIDER env var picks the backend:
    PROVIDER=anthropic   -> Anthropic SDK (claude-*)
    PROVIDER=deepseek    -> OpenAI SDK pointing at DeepSeek (deepseek-chat)
"""

import json
import os
import subprocess
from dataclasses import dataclass

try:
    import readline
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from dotenv import load_dotenv

load_dotenv(override=True)

PROVIDER = os.environ.get("PROVIDER", "anthropic").lower()
MODEL = os.environ["MODEL_ID"]


def _init_client():
    if PROVIDER == "anthropic":
        from anthropic import Anthropic
        if os.getenv("ANTHROPIC_BASE_URL"):
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        return Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL") or None)
    if PROVIDER == "deepseek":
        from openai import OpenAI
        return OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    raise ValueError(f"Unknown PROVIDER: {PROVIDER!r}, expected 'anthropic' or 'deepseek'")


client = _init_client()

SYSTEM = (
    f"You are a coding agent at {os.getcwd()}. "
    "Use bash to inspect and change the workspace. Act first, then report clearly."
)

# Anthropic tool schema
TOOLS_ANTHROPIC = [{
    "name": "bash",
    "description": "Run a shell command in the current workspace.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]

# OpenAI / DeepSeek tool schema (wraps the same params under function.parameters)
TOOLS_OPENAI = [{
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command in the current workspace.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}]


@dataclass
class LoopState:
    # the minimal loop state: history, loop count, and why we continue
    messages: list
    turn_count: int = 1
    transition_reason: str | None = None


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", ">/dev/"]
    if any(item in command for item in dangerous):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: Timeout(120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"
    output = (result.stdout + result.stderr).strip()
    return output[:50000] if output else "(no output)"


# ============================================================
# Anthropic backend
# ============================================================
def _anthropic_turn(state: LoopState) -> bool:
    response = client.messages.create(
        model=MODEL,
        system=SYSTEM,
        messages=state.messages,
        tools=TOOLS_ANTHROPIC,
        max_tokens=8000,
    )
    state.messages.append({"role": "assistant", "content": response.content})

    for block in response.content:
        if getattr(block, "type", None) == "text" and block.text:
            print(block.text)

    if response.stop_reason != "tool_use":
        state.transition_reason = None
        return False

    results = []
    for block in response.content:
        if block.type != "tool_use":
            continue
        command = block.input["command"]
        print(f"\033[33m$ {command}\033[0m")
        output = run_bash(command)
        print(output[:500])
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        })

    if not results:
        state.transition_reason = None
        return False

    state.messages.append({"role": "user", "content": results})
    state.turn_count += 1
    state.transition_reason = "tool_result"
    return True


# ============================================================
# DeepSeek (OpenAI-compatible) backend
# ============================================================
def _deepseek_turn(state: LoopState) -> bool:
    response = client.chat.completions.create(
        model=MODEL,
        messages=state.messages,
        tools=TOOLS_OPENAI,
        max_tokens=8000,
    )
    msg = response.choices[0].message
    finish = response.choices[0].finish_reason

    assistant_msg = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        assistant_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    state.messages.append(assistant_msg)

    if msg.content:
        print(msg.content)

    if finish != "tool_calls" or not msg.tool_calls:
        state.transition_reason = None
        return False

    for tc in msg.tool_calls:
        # arguments comes back as a JSON string in the OpenAI protocol
        args = json.loads(tc.function.arguments)
        command = args["command"]
        print(f"\033[33m$ {command}\033[0m")
        output = run_bash(command)
        print(output[:500])
        state.messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": output,
        })

    state.turn_count += 1
    state.transition_reason = "tool_result"
    return True


# ============================================================
# Dispatcher
# ============================================================
def run_one_turn(state: LoopState) -> bool:
    if PROVIDER == "anthropic":
        return _anthropic_turn(state)
    return _deepseek_turn(state)


def agent_loop(state: LoopState, max_turns: int = 25) -> None:
    while state.turn_count <= max_turns and run_one_turn(state):
        pass


def initial_history() -> list:
    # OpenAI-style providers put system message in the messages list;
    # Anthropic passes it as a separate kwarg, so history stays empty.
    if PROVIDER == "deepseek":
        return [{"role": "system", "content": SYSTEM}]
    return []


if __name__ == "__main__":
    print(f"[provider={PROVIDER}, model={MODEL}]")
    history = initial_history()
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", "quit", ""):
            break
        history.append({"role": "user", "content": query})
        state = LoopState(messages=history)
        agent_loop(state)
        print()
