# harness: extensibility -- inject behavior without touching the loop
"""
s08_hooks_system.py - Hook System

Hooks are extension points around the main loop
they let readers add behavior without rewriting the loop itself

teaching version:
    - sessionstart
    - pretooluse
    - posttooluse

teaching exit-code contract:
    0: continue
    1: block
    2: inject a message

this is intentionally simpler than a production system
the goal here is to teach the extension pattern clearly before introducing event-specific edge cases

key insight:
    extend the agent without touching the loop
"""


import json
import os
import subprocess
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
load_dotenv(override=True)



WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")
HOOK_TIMEOUT = 30 # seconds
# real cc time out
#   TOOL_HOOK_EXECUTION_TIMEOUT_MS = 600000
#   SESSION_END_HOOK_TIMEOUT_MS = 1500 (1.5second)

# workspace trust marker
# hooks only run if this file exists (or SDK mode)

TRUST_MARKER = WORKDIR / ".claude" / ".claude_trusted"

class HookManager:
    """
    load and execute hooks from .hooks.json configuration
    the hook manager does three simple jobs:
    -   read hook definition
    -   run matching commands for an event
    -   aggregate block / message results for the caller
    """

    def __init__(self, config_path: Path = None, sdk_mode: bool = False):
        self.hooks = {"PreToolUse": [], "PostToolUse": [], "SessionStart": []}
        self._sdk_mode = sdk_mode
        config_path = config_path or (WORKDIR / ".hooks.json")
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
                for event in HOOK_EVENTS:
                    self.hooks[event] = config.get("hooks", {}).get(event, [])
                print(f"[Hooks loaded from {config_path}]")
            except Exception as e:
                print(f"[Hook config error: {e}]")

    def _check_workspace_trust(self) -> bool:
        """
        check whether the current workspace is trusted
        the teaching version uses a simple trust marker file
        in SDK mode, trusted treated as implicit
        """

        if self._sdk_mode:
            return True
        return TRUST_MARKER.exists()

    def run_hooks(self, event: str, context: dict= None) -> dict:
        """
        execute all hooks for an event
        return {"blocked": bool, "messages": list[str]}
        - blocked: True if any hook returned exit code 1
        - messages: stderr content from exit-code-2 hooks (to inject)
        """
        result = {"blocked": False, "messages": []}

        # trust gate: refuse to run hooks in untrusted workspaces
        if not self._check_workspace_trust():
            if self.hooks.get(event):
                print(f"  [hooks skipped: workspace not trusted (create {TRUST_MARKER})]")
            return result

        hooks = self.hooks.get(event, [])
        for hook_def in hooks:
            # check matcher (tool name filter for PreToolUse/PostToolUse)
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                if matcher != "*" and matcher != tool_name:
                    continue

            command = hook_def.get("command", "")
            if not command:
                continue

            env = dict(os.environ)
            if context:
                env["HOOK_EVENT"] = event
                env["HOOK_TOOL_NAME"] = context.get("tool_name", "")
                env["HOOK_TOOL_INPUT"] = json.dumps(
                        context.get("tool_input", {}), ensure_ascii=False)[:10000]
                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(
                            context["tool_output"])[:10000]
            
            try:
                r = subprocess.run(
                        command,
                        shell = True,
                        cwd = WORKDIR,
                        env=env,
                        capture_output=True,
                        text = True,
                        timeout = HOOK_TIMEOUT,)
                if r.returncode == 0:
                    # continue silently
                    if r.stdout.strip():
                        print(f"  [hook:{event}] {r.stdout.strip()[:100]}")

                    try:
                        hook_output = json.loads(r.stdout)
                        if "updateInput" in hook_output and context:
                            context["tool_input"] = hook_output["updateInput"]
                        if "additionalContext" in hook_output:
                            result["messages"].append(
                                    hook_output["additionalContext"])
                        if "permissionDecision" in hook_output:
                            result["permission_override"] = (
                                    hook_output["permissionDecision"])
                    except (json.JSONDecodeError, TypeError):
                        pass # stdout was not json -- normal for simple hooks

                elif r.returncode == 1:
                    # block execution
                    result["blocked"] = True
                    reason = r.stderr.strip() or "Blocked by hook"
                    result["block_reason"] = reason
                    print(f"  [hook:{event}] BLOCKED: {reason[:200]}")

                elif r.returncode == 2:
                    # inject message
                    msg = r.stderr.strip()
                    if msg:
                        result["messages"].append(msg)
                        print(f"  [hook:{event}] INJECT: {msg[:200]}")

            except subprocess.TimeoutExpired:
                print(f"  [hook:{event}] Timeout ({HOOK_TIMEOUT}s)")
            except Exception as e:
                print(f"  [hook:{event}] Error: {e}")

        return result



            

# -- Tool implementations (same as s02) --
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


TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."


def agent_loop(messages: list, hooks: HookManager):
    """
    The hook-aware agent loop.
    The teaching version keeps only the clearest integration points:
    SessionStart, PreToolUse, execute tool, PostToolUse.
    """
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_input = dict(block.input or {})
            ctx = {"tool_name": block.name, "tool_input": tool_input}
            
            # -- pretool use hooks
            pre_result = hooks.run_hooks("PreToolUse", ctx)

            # collect PreToolUse hook messages (merged into the single tool_result below)
            hook_notes = list(pre_result.get("messages", []))

            # blocked by hook (exit code 1) or explicit deny override
            if pre_result.get("blocked") or pre_result.get("permission_override") == "deny":
                reason = pre_result.get("block_reason", "denied by hook")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Tool blocked by PreToolUse hook: {reason}",
                    })
                continue

            # -- Execute tool --
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**ctx["tool_input"]) if handler else f"Unknown: {block.name}"
            except Exception as e:
                output = f"Error: {e}"
            print(f"> {block.name}: {str(output)[:200]}")

            # -- PostToolUse hooks --
            ctx["tool_output"] = output
            post_result = hooks.run_hooks("PostToolUse", ctx)

            # merge all hook messages into a single tool_result (one per tool_use_id)
            output = str(output)
            for note in hook_notes:
                output = f"[Hook message]: {note}\n{output}"
            for msg in post_result.get("messages", []):
                output += f"\n[Hook note]: {msg}"
            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": output,
            })
        
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    hooks = HookManager()
    # Fire SessionStart hooks
    hooks.run_hooks("SessionStart", {"tool_name": "", "tool_input": {}})
    history = []
    while True:
        try:
            query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, hooks)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()

