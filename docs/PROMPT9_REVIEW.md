# Prompt 9: missions, planning and scheduling

**Not run on Windows 11. Not run with the real Qwen model (model-written plans were tested only with scripted replies). No real clock, sleep/wake cycle or process restart was exercised.**
This is not a security approval, not a production-readiness statement and not a Windows or real-model verification.
**Implementation complete** (the code exists and its automated tests pass on this Linux machine); **Windows 11 and real-model verification: pending**
(see section 9 and `docs/WINDOWS_TEST_CHECKLIST.md` section 6j). Prompt 6/7/8 are still not Windows-verified either.
Missions run on top of the interim pretrained Qwen model and the existing registered tools. Nothing here is the original from-scratch model, nothing retrains any weights, and mission history is never used for training.

## 1. What was implemented
| Area | What | Files |
|---|---|---|
| Plans | A mission has a unique id, goal, steps with explicit `depends_on`, per-step description, expected result, tool + validated arguments, preconditions, success checks, retry settings, limits (steps, attempts, total seconds) and recorded timestamps/errors. Validation rejects unknown tools, bad arguments, missing dependencies, cycles, secrets, plans over the hard limits and tools hidden by the current mode. Unknown keys such as `state`, `approval`, `approved` are dropped. | `missions/model.py` |
| States | Mission: draft, awaiting_approval, approved, running, paused, completed, failed, cancelled (transition table enforced). Step: pending, running, succeeded, failed, skipped, unverified, unknown, cancelled, blocked. A proposal is never shown as executed: an unrun mission says "NOT been run ... proposal only". | `model.py` |
| Approval | A plan that changes anything needs approval for exactly that plan (SHA-256 plan hash, `--hash` prefix of at least 12 characters). Approval lets the engine **start once** (single use). Every action still follows the permission switches and asks you as usual; approval never enables a permission. Read-only plans need no approval. Editing a plan removes approval. | `runner.py`, `cli_missions.py` |
| Engine | Executes only registered tools through the existing executor (validation, policy, confirmation, audit, emergency stop), re-checking permissions at every step. Act, observe, verify: the request, whether the tool accepted it, the observed result, independent check results and a verdict (verified / failed / inconclusive / unavailable) are recorded per attempt. The first step that is not a clear success stops the mission; dependents are skipped. | `runner.py` |
| Recovery | Intent is saved **before** acting, so a crash leaves the step UNKNOWN and nothing is repeated blindly. Bounded retries (max 2 per step): **since Prompt 13 only for tools on the trusted idempotency list in `desktop/retry.py`**, never because a plan says `retry_safe` (see `docs/PROMPT13_REVIEW.md`); refusals, declined confirmations, failed verification of a change, errors and timeouts of anything not on that list are never retried automatically and leave the step UNKNOWN. `resume` needs an explicit `--retry`, `--accept` or `--skip` per unresolved step. | `runner.py`, `store.py` |
| Control | `pause` and `cancel` are requested through flag files and take effect between steps; a running step cannot be interrupted and the result is still recorded (the message says so). The existing emergency stop (`jarvis desktop stop`, Ctrl+C, hotkey) is honoured before start, between steps and attempts. A second Jarvis process cannot run the same mission (lock file). | `runner.py`, `store.py` |
| Scheduling | Once, daily, weekly and interval schedules; time zones by IANA name, UTC or fixed offset; list/show/edit/pause/resume/cancel/delete; persistent state; a missed-run policy (`skip` or `run_once`, grace window); every occurrence is claimed on disk before running so a restart or second process cannot repeat it. Schedule approval for missions that change things is bound to the plan hash, a run limit (default 5, max 50) and an expiry (default 30 days, max 90), and is re-checked every run. Editing so it fires more often removes the approval. Missions that run code, install software or use the internet can never be scheduled. | `missions/schedule.py` |
| Planner | The local model may only **propose** a draft: its reply is parsed as data (duplicate keys rejected), validated against the real registry and mode, with one repair attempt. It cannot run, approve or schedule anything; the only model-callable mission tool is read-only `mission_status`. | `missions/planner.py`, `missions/tools.py` |
| History | Plans, states, short redacted messages and evidence lines. Never stored: file contents, OCR/screen text, web pages or other untrusted tool text. Inspect with `show`, `history`, `export`; delete with `history --delete`, `delete`, or `memory forget-all`. Distinguishes executions (`execution #N`, trigger manual or schedule) from the plan. | `missions/store.py`, `memory/cli_memory.py` |
| CLI | `jarvis mission new|plan|list|show|review|approve|reject|start|pause|resume|cancel|status|revise|delete|history|export|context`, `jarvis schedule add|list|show|approve|pause|resume|cancel|delete|edit|run-due|watch|history`. No UI redesign. Guest mode refuses. | `cli_missions.py`, `cli.py` |

## 2. Files changed
New: `src/jarvis/missions/{__init__,model,store,runner,schedule,planner,tools}.py`, `src/jarvis/cli_missions.py`,
`tests/{p9_common,test_p9_plans,test_p9_runner,test_p9_schedule,test_p9_integration,test_p9_regression}.py`, this file.
Edited: `extensions.py` (registers `mission_status`), `modes.py` (read tool list), `cli.py` (parsers, dispatch), `memory/cli_memory.py` (`forget-all`), `docs/WINDOWS_TEST_CHECKLIST.md` (6j), `docs/PRIVACY_AND_NETWORK.md`, `CHANGELOG.md`.
No existing test assertion was changed. The built-in tool list is unchanged (a test freezes it); Prompt 9 adds exactly one tool.

## 3. Tests: exact command and results
Command (from the project root, also from a clean extraction of the final archive):
`PYTHONPATH=src python -I -m unittest discover -s tests`
Baseline before Prompt 9 (Prompt 8 archive): 961 tests, OK, 3 skipped.
Final, from a clean extraction of the final archive: **Ran 1130 tests, OK, skipped=4** (about 2 minutes; 1130 = 961 earlier tests + 169 new). Recorded again from the clean extraction of the final archive (section 11).
Skipped (4): the three earlier skips (Stage D opt-in real-model test, PyTorch formula test, Prompt 8 opt-in real-model test) and the new opt-in real-model planning test (`JARVIS_REAL_MODEL_TEST=1`). Nothing was reported as passed for them.
New tests: plans/state/store/redaction 30, engine 42, scheduling 45, planner/CLI/privacy/agent tool 38 (one skipped), regression and docs 14 (169 total).
Two real bugs were found by these tests and fixed: mission ids were sometimes redacted as if they were tokens (random hex), and an accepted UNKNOWN step was run again on resume. A third, a clock set backwards moving the schedule cursor back, was found while writing the scheduler tests.

### Negative controls
Each safeguard was removed on a scratch copy and the Prompt 9 tests had to fail (the clean copy passes):
| Safeguard removed | Result |
|---|---|
| plan approval check | tests fail (4) |
| approval single-use | tests fail (3) |
| tamper check at start (runner) | tests fail (1) |
| tamper detection (store) | tests fail (2) |
| cycle check | tests fail (2) |
| retry without `retry_safe` | tests fail (1) |
| timeout of a change retried | tests fail (1) |
| failed verification of a change retried | tests fail (1) |
| unverifiable result treated as verified | tests fail (2) |
| duplicate-occurrence claim | tests fail (1) |
| schedule cursor allowed to move backwards | tests fail (1) |
| schedule approval expiry | tests fail (1) |
| schedule approval run limit | **first run: NOT caught** (0 failing); test added; second run: caught (1) |
| schedule approval plan-hash match | tests fail (1) |
| schedule needs its own approval | tests fail (2) |
| unattended restriction (network/execute/install risks) | **first run: NOT caught** (0 failing); test added; second run: caught (1) |
| redaction in the store | tests fail (3) |
| accepted UNKNOWN step not re-run | tests fail (1) |
| validation against the registry/mode at start | tests fail (1) |
| emergency stop check at start | tests fail (1) |

20 safeguards. An earlier first pass reported all as caught, but that was invalid (the review document did not exist yet, so one documentation test failed in every copy); the table is from the rerun with a clean baseline. Two safeguards were not covered by any test; tests were added and those two were rerun separately and are now caught. A twenty-first mutation in the first pass was a no-op and is not counted. Mutations were applied to a scratch copy each time and discarded.

## 4. What was mocked, simulated or scripted (versus real)
- REAL: the executor, policy, confirmation, audit, emergency stop, store, runner, scheduler, CLI entry point, files in temporary folders, file locking, JSON persistence, redaction, `zoneinfo` time zones and DST rules.
- SIMULATED: the desktop backend (in-memory); a fake wall clock for scheduling; test-only tools (`t_ok`, `t_flaky_*`, `t_slow_*`, `t_crash`, ...) that give exactly controlled failures, timeouts, ambiguity and crashes; a simulated "restart" is a new Scheduler/Runner object over the same files, and a "crashed process" is an exception raised inside a tool plus a lock file naming a dead pid.
- SCRIPTED: all model replies (`Chat`, `ScriptedModel`). They prove parsing, validation and that a model only proposes; they say **nothing** about Qwen's planning quality.

## 5. Scheduling: when does anything run?
**There is no background service, Task Scheduler entry or startup item.** A schedule fires only while a Jarvis process is checking: `jarvis schedule watch` (polls, bounded to at most 24 h) or `jarvis schedule run-due` (one check).
- **Jarvis closed, PC off, or asleep:** nothing runs. On the next check, occurrences older than the grace window (default 15 minutes) are logged as *missed*. With `--missed skip` (default) they are not run; with `--missed run_once` only the latest missed occurrence runs once, late, and the record says so. Many missed occurrences are never replayed.
- **Restart:** the next-run cursor and a per-occurrence claim (`schedule@time`) are written before a run starts, so the same occurrence is not started twice, even from a second window or after a restored cursor. A claim left "started" by a dead process becomes `interrupted_unknown` and is **not** re-run; the mission is shown paused with the step UNKNOWN.
- **Clock changes:** the cursor never moves backwards; DST gaps move forward, ambiguous times run once.
- Missing time-zone database (common on Windows without the `tzdata` package) gives a clear error; `UTC` and fixed offsets always work.
- A one-time approval of a mission is **not** schedule authority. Recurring changing missions need `schedule approve` (plan hash, run limit, expiry); expiry or the run limit pauses the schedule until you approve again.

## 6. Cancellation and recovery limitations
- A step that has started cannot be interrupted by `pause` or `cancel`; they apply after it, and the message says so. Inline tool calls run on a worker thread with a timeout; after a timeout the underlying operation may still finish, so a timed-out change is recorded as UNKNOWN and is not retried.
- The emergency stop is polled between steps and attempts; it cannot kill an operation already in flight.
- A crash can only leave the step in the "running/unknown" state. Jarvis cannot know whether the action took effect; it asks you (`--retry` may repeat a change).
- Undo of file changes remains the existing checkpoint feature (`jarvis desktop undo`); missions do not add rollback.
- The "is that process still alive" check for lock and claim recovery has a Windows branch that was never run on Windows.

## 7. Questions you asked
- **Can the real Qwen model make valid plans?** **Not tested.** Weights and runtime are not in this environment. The opt-in test `RealModelPlanning` exists and was skipped. Real-model validation is pending.
- **Windows 11:** nothing here was run on Windows.

## 8. Security risks, known bugs and known limitations
- Approval is a typed hash on the same account; anyone with write access to the profile folder can edit mission files, but an edit to the steps changes the plan hash and is detected (refused as tampered). Editing the schedule file could still change a schedule's approval fields; the files are not signed or encrypted.
- Verification is only as good as the plan's checks. A step with no independent check is labelled "tool-reported" and an action the tool cannot verify pauses as "ran, not verified".
- A model-written plan can be wrong or pointless; you must read the review. The validator only guarantees it uses registered tools with valid arguments inside the limits.
- Redaction is pattern-based and can miss unusual secrets; tool output text is not stored at all except a short message and evidence line.
- Retry rules: superseded by Prompt 13. `retry_safe` is now only a request and is never proof; the tool's trusted registration decides. The negative-control rows "retry without `retry_safe`" above describe the Prompt 9 rule, which no longer exists.
- Mission and schedule files are plain JSON (not encrypted), like memory.
- Polling `watch` uses a timer loop in the foreground process; sleep/wake behaviour was not tested.
- Not a security approval: no external review and no fuzzing.

## 9. Not yet verified (pending)
Windows 11: console prompts and Ctrl+C during a mission, lock/claim recovery with real process ids, `zoneinfo` with the `tzdata` package, a real sleep/wake across a schedule, closing the window mid-step. Real model: whether Qwen3-1.7B produces valid plans and how often the repair attempt is needed. Checklist: `docs/WINDOWS_TEST_CHECKLIST.md` section 6j.

## 10. Next recommended milestone
Run section 6j on a real Windows 11 PC, fix what it finds, then run the opt-in real-model planning test and record the validity rate. Prompt 10 (UI and release polish) has not been started.

## 11. Package
`/home/claude/project-jarvis-p15-prompt9.zip` (earlier checkpoints, including `project-jarvis-p15-prompt8.zip`, were not touched). An archive cannot contain its own checksum, so its exact size and full SHA-256 are given in the delivery message and can be recomputed with `sha256sum`.
