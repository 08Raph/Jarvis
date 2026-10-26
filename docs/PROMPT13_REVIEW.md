# Prompt 13 review: mission retry safety, idempotency and execution integrity

> **Superseded in part by Prompt 14 (`docs/PROMPT14_REVIEW.md`).** The 30-tool allowlist described below was audited against the real implementations and cut to 14: the statement that the listed tools start no process except fixed read-only `git` queries was wrong (`system_info`, `monitor_snapshot`, `game_metrics`, `game_status` run `nvidia-smi` from PATH; `git_info` can run repository-configured filters), and tools that open caller-chosen paths can trigger OneDrive downloads. Read section 3 below with that in mind.

Status: **preview only**. Nothing here ran on Windows 11, with the GUI, the real Qwen model, voice hardware, a packaged build or a clean machine. All tests ran on Linux with CPython and simulated desktop backends.

## 1. Baseline
Started from `project-jarvis-prompt12.zip` (SHA-256 `50d7cb5cc2e44f1d7c716886a38d5c634bb11e1e9cdffdda25a93a566402d0ac`, 780,845 bytes), extracted into a clean workspace; a checkpoint copy was taken before any edit.
Command: `PYTHONPATH=src python -I -m unittest discover -s tests`. Baseline: **1277 tests, OK, 6 skipped**.

## 2. What was wrong (confirmed in the code, not assumed)
* `retry_safe` and `retries` were plain fields of the plan file. The runner repeated a step when the plan said `retry_safe: true`, or when the tool's risk label was `observe`. Both are claims made by whoever wrote the plan (a model, or a person editing JSON), not facts about the tool. A mutating tool mislabelled `observe`, or a plan with `retry_safe: true`, could therefore be repeated automatically.
* An error reported by a tool that had already started was recorded as a plain failure, which invites a repeat, although an error does not prove nothing changed.
* The run lock treated a lock owned by the same process id as stale, so a second thread of the same program could take a running mission's lock. (The "already running" state check usually stopped it; the lock itself did not.)
* The mission record was read before the lock was taken, so a worker could act on stale data (approval already spent).
* Tool messages were put into the returned mission and the "reason" text before redaction; redaction happened only when the file was written.
* Success CHECKS were polled up to `retries` times (plan cap 10-20) for any observation tool.

## 3. The policy and where it is enforced
`src/jarvis/desktop/retry.py` is the single decision point. `decide(spec, requested, ceiling)` returns how many automatic repeats are allowed:
1. The tool must be named in `IDEMPOTENT_TOOLS`, a closed, read-only table inside the source (30 tools at the time; reduced to 14 in Prompt 14 — the review claim below was too generous: no writes, no network, no input, no process start except fixed read-only `git` queries; the same arguments give a fresh reading of the same state).
2. The registered spec must also be structurally harmless: risk `observe`, not `no_retry` (new `ToolSpec` field), and no argument-dependent risk (`risk_fn`, `risk_for`) or target binding (`bind`).
3. The plan's `retries` is only a **request**. It can lower the number, never raise it. It is clamped to `MAX_AUTO_RETRIES = 2` (steps) or `MAX_CHECK_POLLS = 20` (checks). Booleans, strings, floats, negatives, `None` or any other bad value mean zero. An unknown or missing tool means zero.
4. `retry_safe` is not read for authorization at all; the history log records that it was ignored.
Names are never used: nothing is eligible because it is called `read_*`, `check_*`, `safe_*` or `*_status`. Extensions cannot register an existing name and none of theirs is in the table.

Enforcement is in the trusted execution path: `MissionRunner._run_step` (steps) and `Runner._observe` in `desktop/task.py` (checks) call `decide`; the GUI, CLI and planner only display or hint. **Every attempt, including an automatic repeat, is a fresh `Executor.run`**: validation, mode, permission, confirmation, sandbox and emergency stop apply again. The mission-wide attempt cap (`max_attempts_total`, hard limit 100) still applies and is clamped; a bad value fails closed.

### Which operations may be repeated, and why
Only the 30 pure reads in the table (for example `system_info`, `file_info`, `read_file`, `file_search`, `list_windows`, `ui_read`, `git_info`, `search_code`, `mission_status`). Deliberately excluded although they only observe: `screenshot`, `read_screen_text`, `observe_screen`, `find_on_screen`, `recheck_screen`, `check_visible` (each capture is a privacy-relevant act the person agreed to once), `read_document` (stores a copy each time), `monitor_watch` (up to 5 minutes), `game_fps_import`/`game_compare` (take a path to a recording, not reviewed), `wait`. Everything that deletes, overwrites, modifies, installs, launches, executes, types, clicks, uses the network, changes permissions or modes, or whose completion cannot be established is **never** repeated automatically. Consequence: a success check that polls a non-listed read tool (such as a screen check) is evaluated once.

## 4. Uncertain outcomes
The existing status vocabulary already had the needed semantics, so no second system was added: `succeeded` (verified), `failed` (verified failure; only for a pure read, or a refusal before anything ran), `unknown` (could not be determined), `cancelled` (cancelled before anything started), plus `unverified` and `blocked`.
* A tool that errors **after it started**, and is not on the list, becomes `unknown` and the mission `paused`. The reason says the result is uncertain and that it was not repeated.
* A timeout of a tool that is not on the list is `unknown`. A timeout of a listed read may be repeated within the limit and ends `unknown` if it keeps timing out.
* Emergency stop during an attempt that had started is `unknown`; before it started, `cancelled`.
* Cancel/pause between attempts of a listed read leaves nothing in flight (`cancelled` / `pending`).
* The intent (`in_flight`: attempt number and kind) is saved **before** each action. After a crash or restart, `recover` marks the running step `unknown` (keeping which kind of attempt was interrupted), pauses the mission, and nothing runs again until the person chooses `--retry`, `--accept` or `--skip` per step. Scheduler ticks and plain `run` calls are refused while a decision is pending.
* Attempt kinds are recorded: `initial`, `auto_retry`, `manual_retry`. A person's `--retry` is logged as `manual_retry_decision` and does not count as, or turn into, an automatic retry. Manual retry of a destructive step still goes through the normal confirmation. Logged events: `retry_policy` (requested/allowed/contract/reason), `auto_retry` (attempt number, reason), `manual_retry_decision`; tool messages are redacted.

## 5. Approval-hash audit
`plan_hash` = SHA-256 of canonical JSON of `{"goal","limits","steps"}` (sorted keys, default separators, `default=str`; documented in the function docstring). It covers the tool, arguments, order, dependencies, preconditions, checks, retries, `retry_safe`, description/expectation and all limits. Not covered: state, approval record, history, executions, context, timestamps. A test changes each execution-relevant field and shows a different hash. The run path refuses a mission whose stored steps no longer match the stored hash, and a consequential mission whose approval hash does not match the plan.
**Trust model (accurate statement):** the plan hash and the approval record are unsigned fields in a JSON file the same user can write. The hash detects accidental edits and edits by a model, the GUI or the CLI that leave the file inconsistent. It does **not** prove who approved, and a process running as the same user can edit steps, hash and approval together. Cryptographic protection would need a key kept outside the user's reach; not implemented, because on a single-user PC the same account can read the key. What protects you even against that edit is that the retry policy does not depend on the hash: a forged "approved" plan still cannot cause an automatic repeat of a non-listed tool, and every action still passes permission and confirmation.

## 6. Behaviour changes (intentional)
* `retry_safe` has no effect on authorization. `retries` on a step whose tool is not in the table is ignored (shown in `mission review` as "automatic retries: 0").
* A changing tool that errors or times out now ends the step `unknown` and pauses the mission (before: `failed` and possibly retried).
* Success checks poll only listed reads.
* `mission review` prints the allowed number and the reason. The planner prompt says the fields are hints.
* The run lock is exclusive between threads of one process as well as between processes; the mission is read only after the lock is held.
* Existing tests that assumed the old behaviour were changed, not deleted: in `tests/test_p9_runner.py`, tests that retried fake `observe`/`modify` test tools via a plan flag now either declare a simulated trusted contract (`P9.contract`) or assert the new, stronger rule (`test_error_after_a_changing_tool_started_is_unknown_not_failed`, `test_a_plan_flag_cannot_make_a_changing_tool_repeat`, `test_retries_of_a_repeatable_read_stop_at_the_limit`, ...). They encoded the unsafe behaviour of section 2.

## 7. Tests
New: `tests/test_p13_retry_safety.py` (55 tests): policy table against the real registry, names that sound safe, bad metadata, ceilings; permitted idempotent retry with log fields; falsely `retry_safe` destructive / network / command-execution / keyboard-mouse / file-modifying tools; a wrong trusted registration; valid approval not overriding the policy; retry-limit bypass (bad types, huge counts, bad totals); modified arguments, retry fields, limits, dependencies and order after approval; forged and stale hashes; single-use approval; hash coverage; timeout unknown; crash after the side effect; restart with an uncertain step; manual vs automatic retry; cancel/pause/emergency stop during attempts; concurrent workers; no secrets in records, logs or reasons.
Commands: `PYTHONPATH=src python -I -m unittest discover -s tests` (all) and `... -p "test_p13*.py"` (new only). Results: see section 9.

### Negative controls (disposable copies; each mutation removes one safeguard; the unmodified copy passes)
Script: `/home/claude/p13/negctl.py` (not shipped). Control: 55 of 55 pass. Mutation -> tests that fail:
| Safeguard removed | Result |
|---|---|
| trusted table ignored | fail (5) |
| risk class check | fail (2) |
| registration `no_retry` | fail (1) |
| argument-dependent risk / bind check | fail (1) |
| retry ceiling | fail (2) |
| bad-type count rejection | fail (1) |
| runner bypasses the policy (plan decides) | fail (11) |
| error-after-start becomes `unknown` | fail (8) |
| mission-wide attempt cap | fail (1) |
| tamper (hash mismatch) refusal | fail (5) |
| intent saved before acting | fail (2) |
| manual retry recorded as manual | fail (1) |
| redaction of tool messages | fail (1) |
| in-process lock registry | fail (1) |
| recovery keeps attempt kind | fail (1) |
| check polling uses the policy | fail (1) |
| consequential/approval check | fail (4) |
| timeout of a change is `unknown` | fail (3) |
Two controls initially passed (argument-dependent risk check; check polling): that showed missing tests, which were added and the controls re-run.

## 8. Remaining risks and platform limits
* **Windows is untested.** Lock-file creation (`O_EXCL`), process liveness (`pid_alive`), file replacement during `save`, and crash behaviour were observed only on Linux. Section 6m of `docs/WINDOWS_TEST_CHECKLIST.md` lists what to run. No Windows crash recovery, file locking or concurrency claim is made.
* The lock is a pid file plus an in-process registry: a pid can be reused after a crash, and a lock file on a network or synced folder may behave differently.
* The "crash" tests simulate death with an exception that bypasses ordinary handlers; they do not kill a real process mid-write.
* The idempotency table is a human review; a listed tool can later change. Tests only check its structure (risk class, no argument-dependent risk), not behaviour. Adding a tool needs the same review.
* `contract()` in tests is a simulation of a trusted registration; the listed-tool path uses test tools, not live desktop reads.
* The approval hash is not a signature (section 5). Same-user write access defeats it.
* An `unknown` step needs the person to look and decide; the tool cannot reconcile it. For file operations the file tools' backups/undo remain the way back.
* Scheduled runs: a schedule claim is made before running and an unknown step pauses the mission, so a schedule does not repeat it; this path was not changed and relies on the existing Prompt 9 tests.
* Not validated: Windows 11, the GUI, real Qwen inference, voice, packaging, clean-machine install.

## 9. Results
Full suite after the changes, working tree: **1333 tests, OK, 6 skipped** (1277 baseline + 56 new; 55 are in `test_p13_retry_safety.py`, and one more was added to `test_p9_runner.py`). The 6 skips are the same as in Prompt 12 (Windows/Qt/runtime items that cannot run on Linux). The Prompt 9 mission suites alone: 170 OK, 1 skipped.
Fresh-extraction result of the delivered archive: recorded in the final report.
