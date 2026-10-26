"""The ONE place that decides whether Jarvis may repeat an action by itself.

Why this exists: a mission step carries `retries` and `retry_safe`, but both are just text in a plan file that a model drafted or a person can edit. A flag
that the thing being checked writes about itself proves nothing. Repeating an action is only safe when the TOOL is known to be safe to repeat, and that
knowledge must come from Jarvis' own trusted registration, not from the plan.

The rule (fail closed):
  * Automatic retries are OFF for every tool unless the tool's name is in IDEMPOTENT_TOOLS below. That table is a closed list written in this file after
    reading what each tool does. It is a read-only view, and an extension (plugin) cannot add to it: extensions cannot register a tool under an existing
    name, and none of their names is in the table.
  * A listed tool must ALSO be structurally harmless: risk `observe`, no arguments-dependent risk, no target binding (it does not pin or touch a
    target) and not marked `no_retry` in its own registration.
  * What the plan says (`retry_safe`, or `retries` on an unlisted tool) is only a REQUEST that can lower the number of repeats, never raise it or grant it.
  * The number of repeats is clamped to MAX_AUTO_RETRIES whatever any file says; a missing, negative, boolean or non-integer value means zero.
  * A repeat is a new call through the normal executor, so permission, mode, validation, confirmation and emergency stop apply to every attempt again.

Not in the table on purpose (Prompt 14 audit, `docs/PROMPT14_REVIEW.md` has the evidence for every tool):
  * anything that opens the CONTENT of, or lists, a path the caller chooses (file_info, file_search, read_file, read_lines, search_code, repo_overview,
    analyze_error, diff_snapshot): on Windows an OneDrive "online only" placeholder is accepted by the path checks, and reading or listing it makes the
    sync client download data (a network request and a local state change) - and that cannot be tested here;
  * anything that starts a program found through PATH or a repository's own configuration (system_info, monitor_snapshot, game_metrics, game_status ->
    nvidia-smi; git_info -> git, whose filter/textconv settings come from the repository), or that writes state when asked to refresh (list_apps -> app_index.json);
  * UI Automation reads (ui_find, ui_read): they run the target application's own accessibility provider code, which is untested on Windows;
  * the screen tools, read_document, monitor_watch, game_fps_import, game_compare, wait (see the earlier list: privacy, stored copies, long runs).
What remains are reads of Jarvis' OWN private records (the argument is a validated id or nothing) and two operating-system enumerations that take no path.

A listed entry is bound to the exact audited handler (module and function name). A tool with the same name but a different handler is not eligible.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from jarvis.desktop import schema as S

MAX_AUTO_RETRIES = 2            # repeats of a mission STEP
MAX_CHECK_POLLS = 20            # repeats of a read-only CHECK (it polls until a state settles); the plan formats already cap it at 10-20

# tool name -> (module, function) of the handler that was audited, and why repeating it cannot change anything.
_IDEMPOTENT = {
    "list_windows": ("jarvis.desktop.tools", "h_list_windows", "Win32 EnumWindows/GetWindowText/GetWindowRect/OpenProcess(query only); no argument; reads titles, not content"),
    "wait_for_window": ("jarvis.desktop.tools", "h_wait_for_window", "polls the same window list for at most 60 s with a stop-aware wait; opens and focuses nothing"),
    "monitor_processes": ("jarvis.monitor.tools", "h_processes", "psutil process names, pid, CPU and memory only (no command lines, paths or users); no writes, no child process"),
    "monitor_self": ("jarvis.monitor.tools", "h_self", "psutil/getrusage numbers about Jarvis itself"),
    "verify_fix": ("jarvis.dev.code_tools", "h_verify_fix", "reads Jarvis' own recorded test-run files; creates nothing"),
    "list_sources": ("jarvis.dev.research", "h_list_sources", "reads Jarvis' own stored research sources"),
    "verify_quote": ("jarvis.dev.research", "h_verify_quote", "string search in a stored source chosen by a validated id"),
    "cite_sources": ("jarvis.dev.research", "h_cite_sources", "formats stored source records chosen by validated ids"),
    "compare_sources": ("jarvis.dev.research", "h_compare_sources", "compares text of stored sources chosen by validated ids"),
    "mission_status": ("jarvis.missions.tools", "h_status", "reads mission and schedule records; the store never repairs or writes on load"),
    "mode_info": ("jarvis.modes", "_h_info", "reads the fixed mode table, the mode file and the permission switches"),
    "vision_status": ("jarvis.desktop.vision", "h_status", "names the installed engines (a PATH lookup only; nothing is started or captured)"),
    "game_profile_list": ("jarvis.monitor.gaming", "h_plist", "reads Jarvis' own profile folder"),
    "game_profile_get": ("jarvis.monitor.gaming", "h_pget", "reads one profile file chosen by a validated id"),
}
IDEMPOTENT_TOOLS = MappingProxyType({k: v[2] for k, v in _IDEMPOTENT.items()})
AUDITED_HANDLERS = MappingProxyType({k: (v[0], v[1]) for k, v in _IDEMPOTENT.items()})

NEVER, IDEMPOTENT = "never", "idempotent"


@dataclass(frozen=True)
class RetryDecision:
    allowed: int          # how many automatic repeats Jarvis may make (0 = none)
    requested: int        # what the plan asked for, after validation (0 when invalid)
    contract: str         # "idempotent" or "never"
    reason: str           # plain words, safe to show and to log

    def as_record(self) -> dict:
        return {"requested": self.requested, "allowed": self.allowed, "contract": self.contract, "reason": self.reason}


def _requested(value) -> tuple[int, str]:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0, "the retry count is missing or not a whole number"
    if value < 0:
        return 0, "the retry count is negative"
    return value, ""


def handler_id(spec) -> tuple[str, str] | None:
    """(module, function name) of the code a tool would run; None when it cannot be determined."""
    h = getattr(spec, "handler", None)
    m, q = getattr(h, "__module__", None), getattr(h, "__qualname__", None)
    return (m, q) if type(m) is str and type(q) is str else None


def ineligible_reason(spec) -> str:
    """'' when the tool has a trusted idempotency contract; otherwise why not. Never raises."""
    if spec is None:
        return "the tool is not registered"
    name = getattr(spec, "name", "")
    if type(name) is not str or name not in IDEMPOTENT_TOOLS:        # exact str only: a look-alike object must not match by custom __eq__/__hash__
        return "this tool has no trusted idempotency contract, so repeating it could change the outcome"
    if handler_id(spec) != AUDITED_HANDLERS.get(name):
        return "the code behind this tool is not the code that was audited"
    if getattr(spec, "no_retry", False):
        return "its registration forbids automatic retries"
    if type(getattr(spec, "risk", None)) is not str or spec.risk != S.OBSERVE:
        return "it is not a pure observation"
    if getattr(spec, "risk_fn", None) is not None or getattr(spec, "risk_for", None) is not None or getattr(spec, "bind", None) is not None:
        return "its risk or target depends on its arguments"
    return ""


def decide(spec, requested, ceiling: int = MAX_AUTO_RETRIES) -> RetryDecision:
    """How many automatic repeats may be made of `spec` given what the plan asked for. The plan can only lower the number."""
    asked, bad = _requested(requested)
    if bad:
        return RetryDecision(0, 0, NEVER, bad + "; no automatic retry")
    why = ineligible_reason(spec)
    if why:
        return RetryDecision(0, asked, NEVER, (why if asked else "no retries requested") + ("; no automatic retry" if asked else ""))
    n = min(asked, ceiling)
    note = "" if n == asked else f" (limited to {ceiling})"
    return RetryDecision(n, asked, IDEMPOTENT, ("pure observation with a trusted idempotency contract" if asked else "no retries requested") + note)
