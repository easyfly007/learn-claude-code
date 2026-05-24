#!/usr/bin/env python3
# harness: the loop -- keep feeding real tool results back into the model
"""
s01_agent_loop.py - the agent loop

this file teaches the smallest useful coding-agent pattern:
	user mesage
		-> model reply
		-> if tool_use: execute tools
		-> write tool_result back to message
		-> continue

it intentionally keeps the loop small, but still makes the loop state explicit
so later chapters can grow from the same structure
"""

import os
import subprocess
from dataclasses import dataclasses
try:
	import readline
	readline.parse_and_bind('set bin-tty-special-chars off')
	readline.parse_and_bind('set input-meta on')
	