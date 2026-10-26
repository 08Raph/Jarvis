# Prompt 14 review: retry allowlist audit, execution integrity, adversarial regression

Status: **preview only**, not security-certified. All tests ran on Linux against a simulated desktop backend. Nothing ran on Windows 11, with the GUI, the real Qwen model, voice hardware, a real process kill, or on a clean machine.

## 1. Baseline
* Input: `project-jarvis-prompt13.zip`, 785,215 bytes, SHA-256 `a859b1ababe8a5b19653b4f011981f6a5ec665d192b9fc9317a3154f6937a140` (matches the Prompt 13 report); `unzip -t` clean. Extracted into a fresh workspace; a separate checkpoint copy was made before any edit.
* Command: `PYTHONPATH=src python -I -m unittest discover -s tests`. Baseline: **1333 tests, OK, 6 skipped**.

## 2. Result of the audit (summary)
The Prompt 13 table listed 30 tools. Reading the real implementations (not names, descriptions or risk labels) showed that **16 of them could not be shown safe to repeat, and that two claims in `docs/PROMPT13_REVIEW.md` were wrong**:
* "no process start except fixed read-only `git` queries" was false: `system_info`, `monitor_snapshot`, `game_metrics` and `game_status` reach `sysinfo.gpu()`, which runs `nvidia-smi` found through `shutil.which` (on Windows that search includes the current folder) and, on Windows, PowerShell.
* `git_info` is not read-only in the required sense: git applies filter/clean commands from the repository's own `.git/config` to `git status`; `--no-ext-diff`, `--no-textconv`, `core.fsmonitor=false` and the ignored global config do not remove that.

**14 tools remain; 16 were removed.** An empty or smaller list is preferable to an unjustified claim, so every doubtful tool was removed rather than argued for.

### Selection rule (what survived)
A tool stays only if (a) its argument cannot name a user path, a program or a remote target (it is nothing, or a validated id for one of Jarvis' own records), (b) a line-by-line read found no write, creation, deletion, program start or network use on any argument, and (c) the same is observed under a tripwire (section 5). Two Windows enumerations are kept on static evidence only and are flagged.

### Why path-reading tools were removed even though they only read
`SafePaths` deliberately accepts Windows cloud placeholders (`_CLOUD_TAG`, OneDrive Files On-Demand; `desktop/paths.py` lines 31-35, 66). Opening such a file for reading, or enumerating a placeholder folder, makes the sync client download data: a network request and a local state change that depends on the target and that cannot be tested here. That is an argument/target-dependent effect, so every tool that opens or lists a caller-chosen path is out. It remains available to the person and to the model for a normal call; only the *automatic repeat* is denied.

## 3. The allowlist audit table
Locations are `src/jarvis/...`. "Static" = read the code; "Tripwire" = run under the recorder in `tests/test_p14_allowlist.py` (Linux, simulated backend); "Windows" = not run.

### 3a. Retained (14)
| Tool | Implementation | Potential side effects checked | Argument-dependent risk | Evidence | Open points |
|---|---|---|---|---|---|
| `list_windows` | `desktop/tools.py::h_list_windows` -> `backend.windows()`, `foreground()`; Windows: `desktop/win32.py::top_windows/window_info` | none found: EnumWindows, IsWindowVisible, GetWindowTextW, GetClassNameW, GetWindowRect, DwmGetWindowAttribute, OpenProcess(query-only) via `process_path` | no arguments | static; tripwire on simulated backend | Windows path never run. Window titles appear in results and in the stored message (redacted by pattern only) |
| `wait_for_window` | `desktop/tools.py::h_wait_for_window` | polls the same list; `env.stop.wait` between polls; returns "not found" as success | text matched against titles only; `timeout` clamped 0.5-60 s | static; tripwire; emergency stop ends it (test) | up to 65 s per attempt; Windows path never run |
| `monitor_processes` | `monitor/tools.py::h_processes` -> `monitor/system.py::RealSource.processes`, `process_readings` | psutil `process_iter(["pid","name"])`, `cpu_percent`, `memory_info`; no command line, path or user is read; 0.3 s sleep | `limit`, `sort` (choices) | static; tripwire (psutil state of this machine) | process names may be sensitive; Windows psutil path never run |
| `monitor_self` | `monitor/tools.py::h_self` -> `RealSource.own` | psutil/getrusage numbers about Jarvis itself | none | static; tripwire | as above |
| `verify_fix` | `dev/code_tools.py::h_verify_fix` | reads `<profile>/code_runs/*.json` written earlier by `run_tests`. **Fixed in this milestone**: it used `_state_dir`, which creates the folder on read; it now only looks | `project` must be an allowed project folder (resolved, not read); `runner` is a choice | static; tripwire; folder-creation regression test | reads whatever is in `code_runs` (Jarvis' own, protected folder) |
| `list_sources` | `dev/research.py::h_list_sources` -> `Sources.all` | read-only; the constructor does not create the folder | none | static; tripwire | stored text may contain anything the person read earlier; returned as data, not stored in mission records |
| `verify_quote` | `dev/research.py::h_verify_quote` -> `Sources.get` | id must match `S[0-9a-f]{8}`; string search | `quote` is data | static; tripwire; hostile id refused | |
| `cite_sources` | `dev/research.py::h_cite_sources` | formats stored records | list of validated ids | static; tripwire; hostile id refused | |
| `compare_sources` | `dev/research.py::h_compare_sources` | text comparison with two fixed regexes | validated ids, `topic` is data | static; tripwire | CPU cost grows with stored text (capped at 2,000,000 chars per source) |
| `mission_status` | `missions/tools.py::h_status` -> `MissionStore.list/load`, `Scheduler.list/_read` | `_read` and `load` never write or repair | `id` goes through `MISSION_ID_RE`; hostile id refused | static; tripwire | |
| `mode_info` | `modes.py::_h_info` -> `current`, `tool_states` | reads `mode.json` and the permission switches | `mode` is looked up in a fixed table | static; tripwire | |
| `vision_status` | `desktop/vision.py::h_status` -> `capabilities` | `shutil.which("tesseract")` is a lookup; nothing is started or captured | none | static; tripwire | |
| `game_profile_list` | `monitor/gaming.py::h_plist` -> `list_profiles` | reads the profile folder | none | static; tripwire | |
| `game_profile_get` | `monitor/gaming.py::h_pget` -> `load_profile` | id must match `SLUG` (`^[a-z][a-z0-9_\-]{1,40}$`) before any path is built | `../../etc/passwd` is refused before the handler runs (test) | static; tripwire | |

### 3b. Removed (16)
| Tool | Why it cannot be shown safe to repeat (from the code) |
|---|---|
| `system_info` | `h_system_info` -> `sysinfo.snapshot` -> `gpu()` runs `nvidia-smi` from `shutil.which` (PATH, and on Windows the current folder) and a PowerShell command; `cpu_usage` sleeps. Starts programs chosen by the environment. |
| `monitor_snapshot` | `Monitor.snapshot` -> `RealSource.gpu` -> `sysinfo.gpu()` (same). Whether it starts a program depends on the `parts` argument and the saved config. |
| `game_metrics` | calls `monitor_for(env).snapshot(...)` -> same `sysinfo.gpu()` path. |
| `game_status` | `_availability` calls `mon.gpu()` -> same path. |
| `list_apps` | `Env.apps(refresh=True)` runs `backend.discover_apps` and **writes `app_index.json`** into the profile (state change) when `refresh` is set. |
| `git_info` | starts `git` found through PATH; repository-local config (`filter.*.clean`, `diff.*`, `include.path`) can run commands during `status`/`diff`. The hardening flags do not cover filters. |
| `file_info` | opens and hashes the target (up to 200 MB): hydrates OneDrive placeholders (network, local state). |
| `file_search` | enumerates a caller-chosen folder; enumerating a cloud placeholder folder makes the sync client fetch its listing. |
| `read_file` | opens the target: same hydration. Also returns file content, which can hold secrets. |
| `read_lines` | opens the target (`code_tools._text_of`): same. |
| `search_code` | opens every file under the folder: same, at scale. |
| `repo_overview` | walks the folder and reads `pyproject.toml`: same. |
| `analyze_error` | the `path` argument switches it from parsing pasted text to opening a file: argument-dependent. |
| `diff_snapshot` | hashes every project file (`manifest`) and `_state_dir` creates a folder on read. |
| `ui_find` | `uia.find` runs `FindAll` on another application's UI tree, executing that application's UI Automation provider; untested on Windows. |
| `ui_read` | same plus pattern reads (`ValuePattern.CurrentValue`) through the target application's provider. |

The tools that were never listed (screen tools, `read_document`, `monitor_watch`, `game_fps_import`, `game_compare`, `wait`, and everything that changes something) are unchanged: no automatic retry.

## 4. Trusted enforcement (re-verified, with two hardenings)
Unchanged design: `desktop/retry.py::decide` is called only inside the trusted path (`MissionRunner._run_step`, `task.Runner._observe`); every attempt is a fresh `Executor.run` (validation, mode, permission, confirmation, sandbox, emergency stop, audit).
New in this milestone:
* **Handler binding.** Each entry is bound to the module and function name of the audited handler (`AUDITED_HANDLERS`). A spec with the same name but different code is not eligible, so a replaced or shadowed tool cannot inherit the trust. Extensions still cannot register an existing name (`Executor._add_spec`).
* **Exact types.** The tool name must be an exact `str` (a look-alike object with a custom `__eq__`/`__hash__` could otherwise match the table), the risk an exact `str`.
* Malformed or missing metadata (`risk`, `no_retry`, `risk_fn`, `risk_for`, `bind`, handler, non-spec objects) all give zero retries.
* A plan, `retry_safe`, a forged or stale hash, a schedule, a saved skill or a plugin cannot enlarge the table or the limits (tests in section 5).

## 5. Tests
New file `tests/test_p14_allowlist.py` (46 tests) plus changes listed in section 7. Labels used in the file: REAL, TRIPWIRE, STATIC, SIMULATED.
* **Tripwire audit of every remaining tool.** Each of the 14 tools is run through the real executor (several argument variants each, including hostile ids, a missing window and a missing source) while all of the following are replaced by recorders that fail the call: writes through `open`/`io.open`/`os.open`, `mkdir`, `remove`, `rename`, `replace`, `utime`, `chmod`, `symlink`, `shutil` copy/move/rmtree, `subprocess`, `os.system`, `socket.connect`, `create_connection`, `getaddrinfo`. The profile and work folders are hashed before and after. The executor's own audit log is the only allowed write. Each tool is also run three times in a row (same status and message) and one is cut off by a timeout.
* **The harness is itself tested** with six SIMULATED hostile tools that look like reads and hide one effect each (write, program, network, mkdir, delete, copy): all six are caught.
* **Static scan** of each handler for write/program/network tokens (shallow, does not follow helpers).
* **Hardening**: different handler under a listed name, look-alike name object, garbage metadata (risk/no_retry/risk_fn/risk_for/bind/handler), non-spec objects, extension taking a listed name, spec replaced after start-up, `plugin_` names, a tool whose risk depends on an argument and whose table entry was added by mistake.
* **Bypass attempts**: scheduled mission with `retry_safe`; the same mission when the tool is (simulated) on the table; plan edited after approval; saved-skill task polling a non-listed check tool; task check on a changing tool.
* **Cancellation / emergency stop** during the wait between attempts, during check polling, during `wait_for_window`, during a running step; cancel between attempts; permission switched off or mode switched *between* attempts (each attempt is authorised again).
* **Secrets** in exception messages, tool results, retry diagnostics and the shown UNKNOWN reason, checked in the mission file, history, audit log and returned record.
* **Restart / UNKNOWN**: skip, accept, retry (recorded as manual), refused decisions, repeated restarts and scheduler ticks.
* **Concurrency**: five threads on five separate runner and store objects; two scheduler ticks for one occurrence; spent single-use approval.
* The docs test fails if this file stops listing a retained or removed tool.

Commands: all: `PYTHONPATH=src python -I -m unittest discover -s tests`; this milestone: `... -p "test_p14*.py"` and `... -p "test_p13*.py"`.

## 6. Negative controls
Script `negctl14.py` (kept outside the archive), disposable copies only; each mutation is applied to a fresh copy, then `test_p13*`, `test_p14*`, `test_p9_runner.py` and `test_p9_schedule.py` are run. Recorded results: section 8.

## 7. Behaviour changes and changed tests
* The automatic-retry allowlist shrinks from 30 to 14 tools. A mission step or check that relied on repeating `file_info`, `read_file`, `system_info`, `file_search` and similar no longer repeats; it runs once. This only affects automatic repeats; the tools themselves work as before.
* `verify_fix` no longer creates `code_runs/` when it is called before any test run.
* `retry.py` gained handler binding and exact-type checks; `ToolSpec` is unchanged.
* Existing tests changed: `tests/test_p13_retry_safety.py` used `file_info` / `plugin_file_info` as the example of a listed tool; they now use `list_windows` (the policy under test is unchanged). `tests/p9_common.py::contract()` now also registers the test tool's handler identity (needed by handler binding) and the flaky test tool gained an `on_call` hook. No assertion was weakened; no test was removed.

## 8. Results
### 8a. Test results
| Run | Command | Result |
|---|---|---|
| Baseline, fresh extraction of the Prompt 13 archive | `PYTHONPATH=src python -I -m unittest discover -s tests` | 1333 tests, OK, 6 skipped, 0 failures |
| Final, working tree | same | **1379 tests, OK, 6 skipped, 0 failures** (46 new in `test_p14_allowlist.py`) |
| Final, fresh extraction of `project-jarvis-prompt14.zip` | same | recorded in the final report |
The 6 skips are the same as in Prompt 12 and 13 (Windows/Qt/runtime items that cannot run on Linux). The milestone tests alone: `-p "test_p13*.py"` 55 OK, `-p "test_p14*.py"` 46 OK.

### 8b. Negative controls (31 mutations, disposable copies, `negctl14.py`)
Method: copy the tree, apply exactly one mutation (the text to replace must occur exactly once), run `test_p13*`, `test_p14*`, `test_p9_runner.py`, `test_p9_schedule.py` (189 tests). The unmodified copy passed 189 of 189. "Detected" means at least one test failed; the failing test names of every mutation are in `docs/PROMPT14_NEGATIVE_CONTROLS.json`.
| Mutation (one safeguard removed) | Area | First run | After fixing the test gap |
|---|---|---|---|
| C1 allowlist: name check AND handler binding both removed (anything with risk observe qualifies) | allowlist enforcement | detected (11 failing) | - |
| C2 allowlist: handler binding alone removed | allowlist enforcement | detected (2 failing) | - |
| C3 allowlist: name check alone removed (handler binding still stands) | allowlist enforcement (defence in depth with C2) | detected (1 failing) | - |
| C4 allowlist: exact-str check removed (look-alike name objects) | allowlist enforcement | detected (1 failing) | - |
| C5 allowlist: a removed dangerous tool (system_info) is put back | allowlist enforcement | detected (2 failing) | - |
| C6 argument-dependent risk/target rejection removed | argument-dependent risk rejection | detected (3 failing) | - |
| C7 risk class check removed | allowlist enforcement | detected (2 failing) | - |
| C8 registration no_retry ignored | allowlist enforcement | detected (2 failing) | - |
| C9 runner repeats by calling the handler directly (bypasses the executor) | central executor enforcement | detected (12 failing) | - |
| C10 executor approves anything (confirmation bypass) | central executor enforcement | detected (2 failing) | - |
| C11 plan retries/retry_safe decide (policy bypassed in runner) | central policy | detected (19 failing) | - |
| C12 check polling ignores the policy | central policy | detected (2 failing) | - |
| C13 UNKNOWN: error after start on a changing tool treated as plain failure | UNKNOWN-outcome handling | detected (13 failing) | - |
| C14 UNKNOWN: timeout of a changing tool recorded as failed | UNKNOWN-outcome handling | detected (4 failing) | - |
| C15 UNKNOWN: intent not saved before acting | UNKNOWN-outcome handling | detected (7 failing) | - |
| C16 UNKNOWN: restart recovery does not mark the running step unknown | UNKNOWN-outcome handling | detected (7 failing) | - |
| C17 forged approval: steps changed after approval are run (hash mismatch ignored) | forged approval resistance | detected (6 failing) | - |
| C18 forged approval: approval hash not compared to the plan on a fresh start | forged approval resistance | detected (3 failing) | - |
| C19 forged approval: approval never used up | forged approval resistance | detected (3 failing) | - |
| C20 forged approval: consequential check always false | forged approval resistance | detected (8 failing) | - |
| C21 cancellation: cancel flag ignored | cancellation propagation | detected (4 failing) | - |
| C22 emergency stop: runner control ignores the stop | emergency-stop propagation | **NOT detected** | detected (1 failing) after adding a test |
| C23 emergency stop: stop.wait no longer checks the stop | emergency-stop propagation | **NOT detected** | detected (3 failing) after adding a test |
| C24 concurrency: run lock not taken | duplicate/concurrent execution | detected (5 failing) | - |
| C25 concurrency: in-process lock registry removed | duplicate/concurrent execution | detected (2 failing) | - |
| C26 concurrency: mission read before the lock is held (stale approval) | duplicate/concurrent execution | **NOT detected** | detected (1 failing) after adding a test |
| C27 duplicate schedule occurrence: cursor not advanced before the run | duplicate/concurrent execution | detected (6 failing) | - |
| C28 secrets: tool messages not redacted in step records | secret redaction | detected (4 failing) | - |
| C29 secrets: mission file saved without redaction | secret redaction | detected (6 failing) | - |
| C30 secrets: audit log not redacted | secret redaction | detected (3 failing) | - |
| C31 read-only tool regression: verify_fix creates the folder again | read-only behaviour of allowlisted tools | detected (1 failing) | - |

**Missed on the first run: C22, C23, C26** (all three are defences that another layer duplicates, which is why the earlier tests did not notice them):
* C22 (mission control point ignores the emergency stop): the executor refuses a stopped call anyway. New test: the mission must not even send the next step to the executor.
* C23 (`EmergencyStop.wait` no longer checks the stop): the executor also abandons a running tool when the stop is set. New tests call the wait, the poll pause and the retry back-off directly and require them to end within seconds.
* C26 (mission read before the lock is held): the concurrency tests lost the race at the lock, never at the stale read. New test makes worker A finish between worker B's first look and B's lock; B must refuse.
All three are detected after the new tests (re-run recorded). One mutation (C3, name check removed while handler binding stands) was detected only because a separate look-alike-name test exists; with ordinary string names the two checks duplicate each other, so that layer alone is not independently proven.


## 9. Remaining risks and exact Windows tests still required
* **Everything Windows-specific is unverified**: `list_windows`/`wait_for_window` (Win32), psutil on Windows, OneDrive behaviour, lock files (`O_EXCL`), process liveness (`pid_alive`), atomic replace, crash behaviour.
* **No real process kill was tested.** "Crash" tests raise an exception that bypasses ordinary handlers inside the process; a real kill during a write is not covered. Do not read these as crash-recovery proof.
* The tripwire shows no effect on Linux with the simulated backend and these fixtures; it cannot show the absence of an effect that needs another platform, another library version, or an argument nobody tried.
* The static scan is shallow; transitive reads were checked by hand once. A later change to a helper can add an effect without touching the handler (the tripwire tests are the guard for that).
* Inline tools that hit their timeout are abandoned on a worker thread, not killed. Retries of a listed read can pile up threads while an earlier call is still running; all listed tools are reads, so this costs time, not state.
* Cancel and pause requests are noticed between attempts and steps, not inside a check-polling loop (up to 20 polls of up to 10 s each, per check); the emergency stop is immediate. Not changed in this milestone.
* Messages stored in mission records can contain window titles, process names or paths; redaction is pattern-based.
* The approval hash and approval record remain unsigned fields in a user-writable file; the retry policy does not depend on them.
* `docs/WINDOWS_TEST_CHECKLIST.md` section 6n lists the tests still required.
