# Agent integration review (Prompt 6, Stage D)

**Not run on Windows. Not run with the real Qwen model.** This is not a security approval, not a production-readiness statement and not a
Windows or real-model verification. The test counts below are results on a Linux machine with a scripted stand-in for the model.

## 1. The path that was traced

```
model text
  -> agent.parse_tool_call      exactly one complete <tool_call>{...}</tool_call>; unfinished <think>, truncation, several calls,
                                duplicate keys, NaN/Infinity, odd tag spellings, unknown tools, bad arguments -> rejected, nothing runs
  -> ToolSpec.validate          unknown/missing/mistyped arguments rejected (so there is no field a model can add to carry "confirm",
                                "settings" or "allowed_folders")
  -> Executor._run              stop check -> validate -> platform -> risk -> least privilege (admin = observe only) -> policy
  -> bind_action                the action is PINNED: real resolved paths / destination / the one chosen program / the one chosen window
  -> policy again               re-judged against the pinned window (Stage D)
  -> confirmation               text and fingerprint come from the pinned action; only True or a matching Approval approves
  -> stop check, bind again     any difference -> "denied ... ask again"; the isolated worker re-pins and compares a digest
  -> audit "start" -> run (inline thread or separate worker process, with a timeout and the emergency stop)
  -> evidence rule              VERIFIED only with ok + verified + recorded evidence
  -> observation()              result returned to the model inside <tool_response>; outside text inside <untrusted_data>
  -> AgentResult.report()       the executor's own report first; the model's summary after it, quoted and labelled unverified
```

There is no tool that changes permissions, settings, allowed folders or the stop (a static test enforces this, and that the agent/tool/
executor code never writes those fields).

## 2. Findings fixed in Stage D (all verified only on Linux with fakes)

| # | Finding | Change | Test (class in `tests/test_desktop_agent_integration.py`) |
|---|---|---|---|
| D1 | A crash in the model runtime (`chat()` raising) escaped `DesktopAgent.run` and lost the report. | Caught: the run ends, the reason is reported. | AgentBoundaryTests |
| D2 | An empty, `None` or non-text reply was treated as a final answer or crashed the parser. | Counted as a malformed reply (bounded to three per run). | AgentBoundaryTests |
| D3 | The text fed back to the model for a rejected reply echoed model-controlled text unescaped. | Neutralised like all other fed-back text. | AgentBoundaryTests |
| D4 | A refused action could be re-asked up to the step limit (12-30 prompts for the same thing). | An exact action the user declined is not asked again in that run (it is answered with "already declined"); three refused/invalid steps in a row stop the run. | AgentBoundaryTests |
| D5 | **Window targets for input/UI/focus actions were not pinned.** The question showed the model's words ("notepad"), not the window, and the handler looked the title up again at run time, so a different window could receive the keys. | `bind_action` resolves the window once, shows its handle, title and program (path when known), puts handle + program + path into the fingerprint and the handler then uses the handle. If the window closes, changes program or another window takes the handle, nothing runs. | WindowTargetBindingTests |
| D6 | The policy (protected programs, terminals, anti-cheat) was evaluated against the window found *before* pinning. | The policy is evaluated again against the pinned window. | WindowTargetBindingTests (a program swap between the two looks) |
| D7 | A task check that could not compare its values (for example `gte` on text) raised and aborted the run, losing the report of the step that had already happened. | The check is inconclusive: the step is UNVERIFIED. | TaskRunnerRobustnessTests |
| D8 | An unwritable task-history file raised after the task finished. | The report is returned with a warning. | TaskRunnerRobustnessTests |
| D9 | A stop reason was hidden when no step had run. | Shown ("No actions were taken. Stopped: ..."). | AgentBoundaryTests |

Negative controls (a scratch copy with one fix removed at a time) fail the matching tests for D1, D4, D5, D6, D7 and D8.

## 3. Behaviour changes to be aware of
- Confirmations for `type_text`, `press_keys`, `mouse_click`, `ui_act` and `focus_window` now show `acts on window [handle] 'title' of program x.exe`, and the
  arguments shown include `hwnd`.
- A window that cannot be found, or that matches several windows, is refused/failed **before** any question.
- Two windows with the same title must be chosen by handle (`list_windows`).
- A window whose handle, program or program path changes between the question and the action causes `denied`; the title is shown but is
  not part of the fingerprint (it changes while people type).

## 4. Required test areas and how each is covered
| Area | Where | Kind |
|---|---|---|
| Malformed responses, multiple calls | `test_desktop_hardening.StrictParserTests`, `AgentBoundaryTests` | FAKE MODEL + REAL parser/executor |
| Prompt injection in untrusted content | `AgentBoundaryTests` (file, window title, UI element), `test_desktop_control` | FAKE MODEL + REAL executor, SIMULATED desktop |
| Attempts to change permissions/security through model output | `AgentBoundaryTests`, `StaticAgentBoundaryTests` | FAKE MODEL; STATIC |
| Arguments changed after confirmation | `test_desktop_hardening.ConfirmationBindingTests`, `WindowTargetBindingTests`, `test_desktop_windows_logic.LaunchBindingTests` | REAL executor |
| Missing or inaccessible targets | `WindowTargetBindingTests`, hardening tests | REAL / SIMULATED |
| Timeouts, cancellation, emergency stop | `AgentBoundaryTests`, `AuditAndRecoveryTests`, `test_desktop_windows_logic.WorkerIntegrationTests` | REAL subprocesses on Linux |
| Audit-log failures | `AuditAndRecoveryTests`, `test_desktop_windows_logic.FailClosedTests` | REAL |
| Tool failures, verification failures | `AgentBoundaryTests`, hardening `TruthfulReportingTests` | REAL |
| Worker errors and recovery | `AuditAndRecoveryTests` (crash, wrong JSON shapes, timeout, stop, then a normal action) | REAL subprocesses |
| Repeated invalid responses, bounded retries | `AgentBoundaryTests` | FAKE MODEL |

## 5. Real-model status
**No real Qwen test was run.** The weights and the runtime (`llama_cpp`) are not available in this environment. Every "model" in the tests is a
list of hand-written replies; it proves what happens *after* a reply, not that Qwen3-1.7B writes such replies.
`RealModelToolCallingTests` is an opt-in test (skipped here; it counts as a skipped test) for your own machine:

```
set JARVIS_REAL_MODEL_TEST=1
set JARVIS_REAL_MODEL_PATH=C:\path\to\qwen3-1.7b-q4_k_m.gguf
python -m unittest discover -s tests -p test_desktop_agent_integration.py -v
```
It uses the simulated desktop, a throw-away folder, declines every question, and prints what the model did so that you can judge it. A pass
would not show the model is reliable or safe.

## 6. Open risks (not fixed)
- A model that adds prose around its call, or writes JSON with a trailing comma, is rejected and costs one of three tries. Real-model behaviour is unknown.
- UI *elements* inside a pinned window are not pinned: `ui_act` selects by name/index at run time, so a window that rearranges between the
  question and the action can change which control is pressed (the window cannot change, the control inside it can).
- The window handle is pinned at the check just before execution; there is still a short gap before the handler runs (and before the worker
  re-pins). It is narrower than before, not closed.
- With `input_confirm = once_per_task`, an approval is remembered per program name, not per window.
- Failure messages of tools can contain outside text (a file or window name in an error); they are neutralised but, unlike successful
  output, not wrapped as `<untrusted_data>` and they do not mark the task as having seen outside text.
- `console_confirm` blocks in `input()`: Ctrl+C sets the stop but does not interrupt the question; the executor honours the stop after the
  answer. The Ctrl+Alt+Pause hotkey has the same behaviour.
- The `INJECTION_HINTS` flag is a keyword heuristic and is advisory only; the protection is the structure (tags, taint, confirmation).
- Everything listed as open in `WINDOWS_INTEGRATION_REVIEW.md` (O1-O13) still applies.

## 7. Prompt 6 status
See the final report of this stage. In short: the Stage A-D work that can be done and tested on Linux is done; everything in checklist
sections 6e, 6f and 6g needs a real Windows 11 PC (and 6g also the real model).
