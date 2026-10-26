"""Computer-control tool layer.

Rules enforced here:
  * Only registered tools can run; nothing is executed from free-form model text.
  * Tools marked ``confirm=True`` run only if the confirm callback approves.
  * Every attempt (success, failure, decline, unsupported) is written to the
    action log with its true outcome. Nothing is reported as done unless it was.
"""
from __future__ import annotations

import ast
import operator
import os
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from jarvis.memory.store import MemoryStore


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Callable[[dict], str]
    confirm: bool = False
    windows_only: bool = False


@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    error: str = ""
    status: str = "ok"  # ok | error | declined | unsupported | unknown_tool


_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UN = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def safe_eval(expression: str) -> float | int:
    """Arithmetic only: numbers, + - * / // % ** and parentheses."""
    if len(expression) > 200:
        raise ValueError("expression too long")

    def ev(node: ast.AST):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(right) > 100:
                    raise ValueError("exponent too large")
                # Bound the size of the RESULT too: nested powers like ((10**100)**100)**100 hang otherwise.
                if isinstance(left, int) and isinstance(right, int) and right > 0 \
                        and abs(left).bit_length() * right > 4096:
                    raise ValueError("result too large")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UN:
            return _UN[type(node.op)](ev(node.operand))
        raise ValueError("unsupported expression")

    return ev(ast.parse(expression, mode="eval"))


def _resolve_exe(exe: str) -> str:
    """Absolute path only. A bare name is looked up in System32, never the current directory,
    so a planted notepad.exe in the working folder cannot be launched in its place."""
    p = Path(exe)
    if not p.is_absolute():
        p = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / exe
    if not p.is_file():
        raise ValueError(f"executable not found: {p}")
    return str(p)


class ToolRegistry:
    def __init__(self, memory: MemoryStore, platform: str | None = None) -> None:
        self._memory = memory
        self._platform = platform or sys.platform
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def run(self, name: str, args: dict | None = None,
            confirm: Callable[[Tool, dict], bool] | None = None) -> ToolResult:
        args = args or {}
        tool = self._tools.get(name)
        if tool is None:
            return self._finish(name, args, ToolResult(False, error=f"unknown tool {name!r}", status="unknown_tool"))
        if tool.windows_only and self._platform != "win32":
            return self._finish(name, args, ToolResult(
                False, error=f"{name} is only supported on Windows", status="unsupported"))
        if tool.confirm and (confirm is None or not confirm(tool, args)):
            return self._finish(name, args, ToolResult(False, error="not confirmed by user", status="declined"))
        try:
            out = tool.handler(args)
        except Exception as exc:  # report the real failure
            return self._finish(name, args, ToolResult(False, error=f"{type(exc).__name__}: {exc}", status="error"))
        return self._finish(name, args, ToolResult(True, output=out))

    def _finish(self, name: str, args: dict, res: ToolResult) -> ToolResult:
        self._memory.log_action(name, args, res.status, res.output if res.ok else res.error)
        return res


def build_default_registry(memory: MemoryStore, app_allowlist: dict[str, str] | None = None,
                           platform: str | None = None) -> ToolRegistry:
    reg = ToolRegistry(memory, platform=platform)
    allow = dict(app_allowlist or {})

    reg.register(Tool("current_time", "Return the local date and time.",
                      lambda a: datetime.now().astimezone().strftime("%A %Y-%m-%d %H:%M:%S %Z")))

    def calculate(a: dict) -> str:
        return str(safe_eval(str(a.get("expression", ""))))
    reg.register(Tool("calculate", "Evaluate an arithmetic expression.", calculate))

    def save_note(a: dict) -> str:
        text = str(a.get("text", "")).strip()
        if not text:
            raise ValueError("note text is empty")
        return f"saved note #{memory.add_note(text)}"
    reg.register(Tool("save_note", "Save a note to local memory.", save_note))

    def list_notes(a: dict) -> str:
        notes = memory.notes()
        return "\n".join(f"#{n['id']}: {n['text']}" for n in notes) or "(no notes)"
    reg.register(Tool("list_notes", "List saved notes.", list_notes))

    def open_app(a: dict) -> str:
        key = str(a.get("name", "")).lower()
        exe = allow.get(key)
        if exe is None:
            raise ValueError(f"{key!r} is not in the allowed app list")
        resolved = _resolve_exe(exe)
        subprocess.Popen([resolved])  # no shell; resolved executable from the allowlist only
        return f"launched {resolved}"
    reg.register(Tool("open_app", "Launch an app from the user's allowlist.", open_app,
                      confirm=True, windows_only=True))
    return reg
