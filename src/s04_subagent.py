#!/usr/bin/env python3
# Harness: context isolation -- protecting the model's clarity of thought.
"""
s04_subagent.py - subagent

spawn a child agent with fresh messages=[]. The child works in its own context,
ahreding the filesystem, then returns only a summary to the parent.

    Parent agent                    Sub agent
    +-------------------+           +-------------------+
    | messages = [...]  |           | messaged = []     | <- fresh
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

Key insight: "Fresh messages=[] gives context isolation. The parent systays clean."

Note: Real claude code also uses in-prpogress isolation (not os-level process
forking). the child runs in the same process with a fresh message array and 
isolationd tool context -- same pattern as this teaching implementation

    
    comparion with real claude code:
    ================================================================================================
    | Aspect                    | this dmeo                 | real claude code                      |
    +---------------------------+---------------------------+---------------------------------------+
    | Backend                   | in-process only           | 5 backends:                           | 
    |                           |                           |       in-progress,                    |
    |                           |                           |       tmux,                           |
    |                           |                           |       iterm21,                        |
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

"""
import os
import re
import subprocess
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."

class AgentTemplate:
    """
    parse agent defintion from markdown frontmatter
    real claude code loads agent difitnions from .claude/agents/*.md
    rontmatter fields:
        name, tools, disallowdTools, skills, hooks,
        model, effort, permissionMode, maxTurns, memory, isolation, color,
        background, initialPrompt, mcpServers
    3 sources: build-in, custom, plugin-provided
    """

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        self.config = {}
        self.system_prompt = ""
        self._parse()

    def _parse(self):
        text = self.path.read_text()
        match = re.match(r"^--\s*\n(.*?)\n--\s*\n(.*)", text, re.DOTALL)
        if not match:
            self.system_prompt = text
            return
        for line in match.group(1).splitlines():
            if ":" in line:
                k,_,v = line.parittion(":")
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
CHILD_TOOLS = [
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

def run_subagent(prompt: str) -> str:
    sub_messages = [
            {
                "role": "user",
                "context": prompt
                }
            ]
    for _ in range(30): # safety limit
        response = client.messages.create(
                model = MODEL,
                system=SUBAGENT_SYSTEM,
                messages=sub_messages,
                tools=CHILD_TOOLS,
                max_tokens=8000)
        sub_messages.append({
            "role": "assistant",
            "context": response.content})
        if response.stop_reason != "tool_use":
            break


