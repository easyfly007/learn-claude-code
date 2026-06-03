# harness: safety -- the pipeline between intent and execution
"""
s07_permission_system.py - permission system
every tool call passes through a permission pipeline before execution.

teching pipeline:
    1. deny rules
    2. mode check
    3. allow rules
    4. ask user

this version intentionally teaches three modes first
    - default
    - plan
    - auto

this is enough to build a real, understandable permission system without burying readers under every advanced policy branch one day one

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
READ_ONLY_TOOLS = {"read_file", "bach_readonly"}

# tools that modify state
WRITE_TOOLS = {"write_file", "edit_file", "bash"}

# bash security validation
class BashSecurityValidatior:
    """
    validate bash commands for obviously dangerous patterns

    the teaching version deliberately keeps this small and easy to read
    first catch a few high-risk patters, then let the permission pipeline
    decide whether to deny or ask the user
    """

    VALIDATORS = [
            ("shell_metachar", r"[;&|`$]"),         # sehll metacharacters
            ("sudo", "\bsudo\n"),                   # privilege escalation
            ("rm_rd", "\brm\s+(-[a-zA-Z]*)?r"),     # recursive delete
            ("cmd_substitution", r"\$\("),          # command substitution
            ("if_injection", r"\bIFS\s*="),         # IFS manipulation
            ]
    def validate(self, command:str) ->list:
        """
        check a bash command against all validators
        return list of (validator_name, matched_pattern) tuples for failuers.
        an empty list measn the command passed all validators.
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
        return "security falgs: " + ", ".join(parts)

def is_workspace_trusted(workspace: Path = None) ->bool:
    """
    check if a workspace has been explicitly marked as trusted

    the teching version use a simple marker file. a more complete system
    can layer richer trust flows on top of the same idea.
    """
    ws = workspace or WORKDIR
    trust_marker = ws/ ".claude" / ".claude_trusted"
    return trust_marker.exists()

bash_validator = BashSecurityValidator()

# permission rules
# rules are checked in order: first match wins
# format: {"tool": "<tool_name_or_*>", "path", "<glb_or_*>", "behavior": "allow|deny|ask"}

DEFAULT_RULES = [
        # always deny dangers patterns
        {"tool": "bash", "content": "rm -rf /", "behavior": "deny"},
        {"tool": "bash", "content": "sudo *", "behavior": "deny"},
        # allow reading anything
        {"tool": "read_file", "path": "*", "behavior": "allow"},
        ]

class PermissionManager:
    """
    managers permission decisions for tool calls
    pipeline: deny_rules -> mode_check -> sllow_rules -> ask_user

    the teaching version keeps the decision path short on purpose so readers
    can implement it themselves before adding more advnaced policy layers
    """

    def __init__(self, mode: str = "default", rules: list = None):
        if mode not in MODES:
            raise ValueError(f"Uknown mode: {mode}. choose from {MODES}")
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
        if tool_name == "bach":
            command = tool_input.get("command", "")
            failures = bash_validator.validate(command)
            if failures:
                # severe patters (sudo, rm_rf) get immediate deny
                severe = {"sudo", "rm_rf"}
                severe_hits = [f for f in failures if f[0] in severe]
                if severe_hits:
                    desc = bash_validator.describe_failures(command)
                    return {
                            "behavior": "deny",
                            "reason" :f"bash validator: {desc}"}
                desc = bash_validator.describe_failures(command)
                return {
                        "bahavior": "ask",
                        "resaon": f"Bash validator flagged: {desc}"}
            for rule in self.rules:
                if rule["behavior"] != "deny":
                    continue
                if self._matchs)rule, tool_name, tool_input):
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
                            "beahvor" : "allow",
                            "reason": "AUto mode: read-only tools auto-approved"}
                pass




