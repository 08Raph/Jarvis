"""Structured tool schemas and strict argument validation. Unknown arguments, wrong types and out-of-range values are
rejected before anything runs. The model can only fill in these fields; it cannot add behaviour."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable


class ArgError(ValueError):
    pass


@dataclass(frozen=True)
class Arg:
    name: str
    type: str                       # str | int | float | bool | list[str]
    description: str = ""
    required: bool = True
    default: Any = None
    max_len: int = 1000             # for str / list items
    max_items: int = 50             # for lists
    min: float | None = None
    max: float | None = None
    choices: tuple | None = None
    no_newlines: bool = False

    def check(self, value):
        t = self.type
        if t == "str":
            if not isinstance(value, str):
                raise ArgError(f"'{self.name}' must be text")
            if len(value) > self.max_len:
                raise ArgError(f"'{self.name}' is too long (max {self.max_len} characters)")
            if "\x00" in value:
                raise ArgError(f"'{self.name}' contains a NUL character")
            if self.no_newlines and ("\n" in value or "\r" in value):
                raise ArgError(f"'{self.name}' must be a single line")
        elif t == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ArgError(f"'{self.name}' must be a whole number")
        elif t == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ArgError(f"'{self.name}' must be a number")
            value = float(value)
            if not math.isfinite(value):
                raise ArgError(f"'{self.name}' must be a finite number (not NaN or infinity)")
        elif t == "bool":
            if not isinstance(value, bool):
                raise ArgError(f"'{self.name}' must be true or false")
        elif t == "list[str]":
            if not isinstance(value, list) or len(value) > self.max_items:
                raise ArgError(f"'{self.name}' must be a list of at most {self.max_items} items")
            for v in value:
                if not isinstance(v, str) or len(v) > self.max_len or "\x00" in v:
                    raise ArgError(f"every item of '{self.name}' must be text up to {self.max_len} characters")
        else:  # pragma: no cover - programming error
            raise ArgError(f"unknown arg type {t}")
        if self.min is not None and value < self.min or self.max is not None and value > self.max:
            raise ArgError(f"'{self.name}' must be between {self.min} and {self.max}")
        if self.choices is not None and value not in self.choices:
            raise ArgError(f"'{self.name}' must be one of: {', '.join(map(str, self.choices))}")
        return value

    def json_schema(self) -> dict:
        m = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}
        d: dict = {"type": m[self.type]} if self.type in m else {"type": "array", "items": {"type": "string"}}
        if self.description:
            d["description"] = self.description
        if self.choices:
            d["enum"] = list(self.choices)
        return d


# Risk levels, lowest to highest. Policy decides what each one needs.
OBSERVE = "observe"          # reads state: system info, list windows, read a file inside allowed folders
LAUNCH = "launch"            # starts a program the user can see
MODIFY = "modify"            # creates something new, or changes something with an automatic backup
INPUT = "input"              # sends keystrokes / clicks / UI actions to another program
DESTRUCTIVE = "destructive"  # overwrites, moves away, deletes (always with backup where possible)
INSTALL = "install"          # opens an installer
EXECUTE = "execute"          # runs code (tests, a build, a sandboxed snippet, a plugin): always asks first
NETWORK = "network"          # talks to the internet (fetching a page, a search): always asks first, showing exactly what is sent
EXTERNAL = "external"        # could send something off this PC (none implemented; reserved)
ADMIN = "admin"              # needs administrator rights: never performed by Jarvis
RISKS = (OBSERVE, LAUNCH, MODIFY, INPUT, DESTRUCTIVE, INSTALL, EXECUTE, NETWORK, EXTERNAL, ADMIN)
ALWAYS_CONFIRM = {DESTRUCTIVE, INSTALL, EXECUTE, NETWORK, EXTERNAL}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    category: str                  # permission category, see policy.CATEGORIES
    risk: str
    args: tuple[Arg, ...] = ()
    handler: Callable[..., "Outcome"] | None = field(default=None, compare=False, repr=False)
    timeout: float = 20.0
    untrusted_output: bool = False # output contains text from files/web/screen/other apps
    windows_only: bool = False
    risk_for: Callable[[dict], str] | None = field(default=None, compare=False, repr=False)  # args-dependent risk
    # Hooks for tools added by extensions (code, research, plugins). Both receive the Env.
    #   risk_fn(args, env) -> risk    : args-dependent risk (a new file is MODIFY, replacing one is DESTRUCTIVE ...)
    #   bind(args, env) -> (pinned_args, effects, extra) : resolve what the arguments REALLY point at, so the confirmation shows
    #   it and a change before running forces a new approval. May raise PathRejected / PathMissing.
    risk_fn: Callable | None = field(default=None, compare=False, repr=False)
    bind: Callable | None = field(default=None, compare=False, repr=False)
    # Trusted registration can forbid automatic retries outright (jarvis.desktop.retry also refuses every tool that is not on its closed list).
    no_retry: bool = False

    def validate(self, args: dict | None) -> dict:
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ArgError("arguments must be an object")
        args = dict(args)
        known = {a.name for a in self.args}
        extra = set(args) - known
        if extra:
            raise ArgError(f"unknown argument(s) for {self.name}: {', '.join(sorted(extra))}")
        out = {}
        for a in self.args:
            if a.name in args:
                out[a.name] = a.check(args[a.name])
            elif a.required:
                raise ArgError(f"{self.name} needs '{a.name}'")
            else:
                out[a.name] = a.default
        return out

    def effective_risk(self, args: dict) -> str:
        return self.risk_for(args) if self.risk_for else self.risk

    def json_schema(self) -> dict:
        return {"name": self.name, "description": self.description, "risk": self.risk,
                "parameters": {"type": "object", "additionalProperties": False,
                               "properties": {a.name: a.json_schema() for a in self.args},
                               "required": [a.name for a in self.args if a.required]}}


@dataclass
class Outcome:
    """What a tool really observed. `verified` is True only when a check AFTER the action confirmed the result,
    False when the check showed it did not happen, None when no check is possible (then nothing may be claimed)."""
    ok: bool
    summary: str
    verified: bool | None = None
    evidence: str = ""
    data: dict = field(default_factory=dict)
    untrusted_text: str = ""        # text from outside (file/screen/window) - shown to the model only as data
    checkpoint: str | None = None   # backup id, if one was made

    def to_dict(self) -> dict:
        return {"ok": self.ok, "summary": self.summary, "verified": self.verified, "evidence": self.evidence,
                "data": self.data, "untrusted_text": self.untrusted_text, "checkpoint": self.checkpoint}

    @classmethod
    def from_dict(cls, d: dict) -> "Outcome":
        return cls(**{k: d.get(k) for k in ("ok", "summary", "verified", "evidence", "untrusted_text", "checkpoint")},
                   data=d.get("data") or {})
