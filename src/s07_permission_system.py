# harness: safety -- the pipeline between intent and execution
"""
s07_permission_system.py - permission system
every tool call passes through a permission pipeline before execution.

teaching pipeline:
    1. deny rules
    2. mode check
    3. allow rules
    4. ask user

this version intentionally teaches three modes first
    - default
    - plan
    - auto

this is enough to build a real, understandable permission system without burying readers under every advanced policy branch on day one

key insight: "safety is a pipeline, not a boolean."
"""
import json
import os
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# -- permission modes --
# teaching version starts with three clear modes first
MODES = ("default", "plan", "auto")
READ_ONLY_TOOLS = {"read_file", "bash_readonly"}

# tools that modify state
WRITE_TOOLS = {"write_file", "edit_file", "bash"}

# bash security validation
class BashSecurityValidator:
    """
    validate bash commands for obviously dangerous patterns

    the teaching version deliberately keeps this small and easy to read
    first catch a few high-risk patterns, then let the permission pipeline
    decide whether to deny or ask the user
    """

    VALIDATORS = [
            ("shell_metachar", r"[;&|`$]"),         # shell metacharacters
            ("sudo", r"\bsudo\b"),                  # privilege escalation
            ("rm_rf", r"\brm\s+(-[a-zA-Z]*)?r"),    # recursive delete
            ("cmd_substitution", r"\$\("),          # command substitution
            ("if_injection", r"\bIFS\s*="),         # IFS manipulation
            ]
    def validate(self, command:str) ->list:
        """
        check a bash command against all validators
        return list of (validator_name, matched_pattern) tuples for failures.
        an empty list means the command passed all validators.
        """
        failures = []
        for name, pattern in self.VALIDATORS:
            if re.search(pattern, command):
                failures.append((name, pattern))
        return failures

    def is_safe(self, command:str)->bool:
        """
        convenience: return true only if no validators triggered
        """
        return len(self.validate(command)) == 0

    def describe_failures(self, command: str)->str:
        """
        Human-readable summary of validation failures
        """
        failures = self.validate(command)
        if not failures:
            return "No issues detected"
        parts = [f"{name} (pattern: {pattern})" for name, pattern in failures]
        return "security flags: " + ", ".join(parts)

def is_workspace_trusted(workspace: Path = None) ->bool:
    """
    check if a workspace has been explicitly marked as trusted

    the teaching version uses a simple marker file. a more complete system
    can layer richer trust flows on top of the same idea.
    """
    ws = workspace or WORKDIR
    trust_marker = ws/ ".claude" / ".claude_trusted"
    return trust_marker.exists()

bash_validator = BashSecurityValidator()

# permission rules
# rules are checked in order: first match wins
# format: {"tool": "<tool_name_or_*>", "path", "<glob_or_*>", "behavior": "allow|deny|ask"}

DEFAULT_RULES = [
        # always deny dangerous patterns
        {"tool": "bash", "content": "rm -rf /*", "behavior": "deny"},
        {"tool": "bash", "content": "sudo *", "behavior": "deny"},
        # allow reading anything
        {"tool": "read_file", "path": "*", "behavior": "allow"},
        ]

class PermissionManager:
    """
    manages permission decisions for tool calls
    pipeline: deny_rules -> mode_check -> allow_rules -> ask_user

    the teaching version keeps the decision path short on purpose so readers
    can implement it themselves before adding more advanced policy layers
    """

    def __init__(self, mode: str = "default", rules: list = None):
        if mode not in MODES:
            raise ValueError(f"Unknown mode: {mode}. choose from {MODES}")
        self.mode = mode
        self.rules = rules or list(DEFAULT_RULES)
        # simple denial tracking helps surface when the agent is repeatedly
        # asking for action the system will not allow
        self.consecutive_denials = 0
        self.max_consecutive_denials = 3

    def check(self, tool_name: str, tool_input: dict) -> dict:
        """
        returns: {"behavior": "allow" | "deny" | "ask" | "reason": str}
        """
        if tool_name == "bash":
            command = tool_input.get("command", "")
            failures = bash_validator.validate(command)
            if failures:
                # severe patterns (sudo, rm_rf) get immediate deny
                severe = {"sudo", "rm_rf"}
                severe_hits = [f for f in failures if f[0] in severe]
                if severe_hits:
                    desc = bash_validator.describe_failures(command)
                    return {
                            "behavior": "deny",
                            "reason" :f"bash validator: {desc}"}
                desc = bash_validator.describe_failures(command)
                return {
                        "behavior": "ask",
                        "reason": f"Bash validator flagged: {desc}"}

        for rule in self.rules:
            if rule["behavior"] != "deny":
                continue
            if self._matches(rule, tool_name, tool_input):
                return {
                        "behavior": "deny",
                        "reason": f"blocked by deny rule:{rule}"
                        }

        if self.mode == "plan":
            if tool_name in WRITE_TOOLS:
                return {
                        "behavior": "deny",
                        "reason": "Plan mode: write operations are blocked"}
            return {"behavior": "allow", "reason": "Plan mode: read-only allowed"}

        if self.mode == "auto":
            # auto mode: auto-allow read-only tools, ask for writes
            if tool_name in READ_ONLY_TOOLS or tool_name == "read_file":
                return {
                        "behavior" : "allow",
                        "reason": "Auto mode: read-only tools auto-approved"}
            pass
            
        # step 3, allow rules
        for rule in self.rules:
            if rule["behavior"] != "allow":
                continue
            if self._matches(rule, tool_name, tool_input):
                self.consecutive_denials = 0
                return {
                        "behavior": "allow",
                        "reason": f"matched allow rule: {rule}"}

        # step 4: ask user (default behavior for unmatched tools)
        return {
                "behavior": "ask", 
                "reason": f"no rule matched for {tool_name}, asking user"}

    def ask_user(self, tool_name: str, tool_input: str)->bool:
        """
        interactive approval prompt, returns true if approved
        """
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        print(f" [permission] {tool_name}: {preview}")
        try:
            answer = input("  Allow? (y/n/always):").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if answer == "always":
            self.rules.append({"tool": tool_name, "path": "*", "behavior": "allow"})
            self.consecutive_denials = 0
            return True
        if answer in ("y", "yes"):
            self.consecutive_denials = 0
            return True
        self.consecutive_denials += 1
        if self.consecutive_denials >= self.max_consecutive_denials:
            print(f" [{self.consecutive_denials} consecutive denials --"
            "consider switching to plan mode]")
        return False

    def _matches(self, rule:dict, tool_name:str, tool_input: dict)->bool:
        """ check if a rule matches the tool call """
        # tool name match
        if rule.get("tool") and rule["tool"] != "*":
            if rule["tool"] != tool_name:
                return False
        # path pattern match
        if "path" in rule and rule["path"] != "*":
            path = tool_input.get("path", "")
            if not fnmatch(path, rule["path"]):
                return False
            # content pattern match (for bash commands)
        if "content" in rule:
            command = tool_input.get("command", "")
            if not fnmatch(command, rule["content"]):
                return False

        return True


# -- Tool implementations --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
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
                },
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

SYSTEM = f"""You are a coding agent at {WORKDIR}. Use tools to solve tasks.
The user controls permissions. Some tool calls may be denied."""


def agent_loop(messages: list, perms: PermissionManager):
    """
    the permission-aware agent loop
    
    for each tool call:
        1. LLM requests tool use
        2. permission pipeline checks: deny_rule, -> mode, -> allow_rule, -> ask
        3. if allowed: execute tool, return result
        4. if denied: return rejection message to LLM
    """
    while True:
        response = client.messages.create(
                model = MODEL,
                system = SYSTEM,
                messages = messages,
                tools = TOOLS,
                max_tokens = 8000,
                )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            decision = perms.check(block.name, block.input or  {})
            if decision["behavior"] == "deny":
                output = f"permission denied: {decision['reason']}"
                print(f" [DENIED] {block.name}: {decision['reason']}")
            elif decision['behavior'] == "ask":
                if perms.ask_user(block.name, block.input or {}):
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**(block.input or {})) if handler else f"unknown {block.name}"
                    print(f">{block.name}: {str(output)[:200]}")
                else:
                    output = f"permission denied by user for {block.name}"
                    print(f"  [USER DENIED] {block.name}")


            else:  # allow
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**(block.input or {})) if handler else f"Unknown: {block.name}"
                print(f"> {block.name}: {str(output)[:200]}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output),
            })
        messages.append({"role": "user", "content": results})

if __name__ == "__main__":
    # Choose permission mode at startup
    print("Permission modes: default, plan, auto")
    mode_input = input("Mode (default): ").strip().lower() or "default"
    if mode_input not in MODES:
        mode_input = "default"
    perms = PermissionManager(mode=mode_input)
    print(f"[Permission mode: {mode_input}]")
    history = []
    while True:
        try:
            query = input("\033[36ms07 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # /mode command to switch modes at runtime
        if query.startswith("/mode"):
            parts = query.split()
            if len(parts) == 2 and parts[1] in MODES:
                perms.mode = parts[1]
                print(f"[Switched to {parts[1]} mode]")
            else:
                print(f"Usage: /mode <{'|'.join(MODES)}>")
            continue
        # /rules command to show current rules
        if query.strip() == "/rules":
            for i, rule in enumerate(perms.rules):
                print(f"  {i}: {rule}")
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history, perms)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
