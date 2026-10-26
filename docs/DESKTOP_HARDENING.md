# Desktop control: focused security hardening (p15 hardening pass)

This is a **limited** pass over the p15 desktop-control layer. It is not a security approval, and the project is **not** described
as production-ready. Nothing in this pass was run on Windows.

## Scope

In scope (exactly four areas):

1. **Protect Jarvis's own data from the file tools.**
2. **Bind a confirmation to the exact action that runs.**
3. **Parse model tool calls strictly.**
4. **Report execution truthfully.**

Deliberately **not touched**: keyboard input, mouse input, UI automation, game interaction, anti-cheat detection, screenshots/OCR,
app discovery, and the Windows backend modules. No new features were added.

## What changed

### 1. Internal data protection (`paths.py`, `files.py`, `checkpoints.py`, `policy.py`, `tools.py`, `cli_desktop.py`)
- Jarvis's own folders (the profile folder; for a named profile also the shared data folder; the model folders) are *protected*:
  no file tool may read, write, edit, copy, move, rename, delete or list inside them, and none may change, copy or move a folder
  that *contains* them. `file_search` skips them.
- `jarvis desktop allow-folder` and a hand-edited permissions file cannot allow a protected folder or one that contains it.
- Symbolic links, junctions, mount points and unknown reparse points anywhere below an allowed folder are refused (OneDrive-style
  cloud placeholders and WOF/dedup tags are the only reparse points let through - **untested on Windows**).
- Files with more than one hard link are refused (a hard link could be another name for a protected file).
- When a check cannot be completed (the OS error, a symlink loop, an unresolvable name) the path is refused.
- Paths containing control characters or invisible/direction-override characters are refused (zero-width *joiners* used in real
  Persian/Hindi/emoji names are allowed).
- Writes go through a **new, uniquely named, exclusively created** temp file and the target is re-validated immediately before the
  replace. (p15 used the fixed name `<file>.jarvis-tmp`, so a link planted at that name redirected the write.)
- `undo` validates every location it will touch, so a tampered checkpoint record can no longer move a file in from outside the
  allowed folders.

### 2. Confirmation bound to the exact action (`tools.py: bind_action`, `executor.py`)
- After validation the action is *pinned*: paths become the real resolved paths, a folder given as a copy/move destination
  becomes the file that will really be created, and the risk is computed from those values.
- The text the user sees, the arguments handed to the tool, and the audit record all come from that one pinned object. Long text is
  shown with its length and a SHA-256 digest; control and direction-override characters are escaped.
- `ConfirmRequest.args` is a private copy (editing it changes nothing) and carries a `fingerprint` of the action, its risk and the
  state (size, mtime) of the files it touches.
- Only the exact boolean `True`, or `Approval(True, fingerprint)` for *this* fingerprint, approves. Strings such as `"no"`,
  numbers, dicts, `None`, an approval for another fingerprint, or a callback that raises are all refusals.
- Just before running, the action is pinned again. If anything differs (target, contents, risk) nothing runs and the status is
  `denied` with "ask again for a fresh approval". This also covers actions that needed no prompt.
- The isolated worker re-pins the action itself and runs only if its digest matches the parent's.

### 3. Strict tool-call parsing (`agent.py: parse_tool_call`)
A model reply is either **exactly one** `<tool_call>{...}</tool_call>` block, or plain text with no trace of a tool call.
Everything else is a safe failure that is fed back to the model (three bad replies stop the run):
unfinished `<think>` blocks (their text is never used, as a call *or* an answer), text before a lone `</think>`, truncated calls,
more than one call, text around a call, a call quoted inside an answer, odd spellings of the tag, NaN/Infinity/overflowing
numbers, duplicate JSON keys, extra top-level keys, wrong shapes, oversized calls, unknown tools, and arguments that fail the
registered schema (checked **before** dispatch; the executor validates again). `Arg` float checking also rejects NaN/Infinity,
which previously slipped past the min/max comparison.

### 4. Truthful reporting (`executor.py`, `agent.py`, `task.py`)
- `verified` requires the tool to report `ok`, `verified=True` **and** record non-empty evidence; otherwise `unverified`.
- Statuses stay distinct in every report: VERIFIED, UNVERIFIED, FAILED, DENIED, DECLINED, INVALID, UNSUPPORTED, TIMED OUT (result
  UNKNOWN), STOPPED before it started (nothing was done) and STOPPED while running (result UNKNOWN). `StepResult.started` records
  whether the action had begun.
- A missing file is reported as FAILED (not found), not DENIED.
- The executor's report always comes first. The model's own summary comes after it, quoted and labelled "not verified by Jarvis";
  its use of the executor's status words and header is neutralised so it cannot pass for the report, and a success-sounding
  summary that the checks do not support gets a warning. (The success detection is a keyword heuristic and can be wrong either way;
  the guarantee is the structure, not the keyword match.)
- Text from tools (messages, evidence, keys) is neutralised before the model sees it, so it cannot forge the response envelope.
- Task runner: a step that times out makes the task `unknown` (no guess, no automatic rollback); a check that could not be run
  leaves the step UNVERIFIED instead of "NOT DONE"; mid-step cancellation is labelled CANCELLED.

## Behaviour changes to existing functionality (deliberate)
- `preview` now reports a bad path as `ok: false` up front (it used to say `ok: true` and fail at run time). One existing test
  assertion was changed for this.
- Two tool calls in one reply are rejected (p15 silently ran the first). One existing test assertion was changed for this.
- Symlinks/junctions/hard links inside an allowed folder are refused even when they point to somewhere harmless.
- A copy/move to an existing folder is now judged by the file it will really create (a new file there is `modify`, not `destructive`).
- A folder that contains Jarvis's data cannot be an allowed folder, and a data folder placed inside an allowed folder is hidden.

## How each kind of test should be read
| Label | Meaning |
|---|---|
| **REAL** | real files, real symlinks/hard links on the Linux test machine, the real executor, the real worker entry point and a real subprocess worker |
| **MOCKED** | the OS answer is replaced (fake Windows reparse-point `lstat`, replaced tool handlers). Proves our decision logic only |
| **ADVERSARIAL STRINGS** | hand-written model outputs through the real parser/agent loop. **No language model is involved** |
| **STATIC** | reading the code only |

See the header of `tests/test_desktop_hardening.py` and each class docstring. Run everything with
`python -m unittest discover -s tests`.

## Known limits (not fixed, or not provable here)
- **Time-of-check/time-of-use is narrowed, not eliminated.** The target is re-validated right before the replace, but there is no
  handle-based (`openat`/directory-handle) enforcement, so a local process that can write inside an allowed folder could still win
  a race in that tiny window.
- Window/UI-element targets for input and UI actions were **not** bound the same way in this pass; their confirmations were
  as in p15. (Stage D later pinned the target *window*; UI elements inside it are still selected at run time - see `AGENT_INTEGRATION_REVIEW.md`.)
- Windows junctions, reparse tags, OneDrive placeholders, 8.3 short names, case-insensitivity and alternate data streams are
  handled by code that has **never run on Windows**.
- The strict parser has **not** been tried against the real Qwen model. A model that habitually adds prose around its call will
  be rejected and will cost retries; that needs a real-model test.
- `Settings.save` still uses a fixed `.tmp` name, but it lives in the protected profile folder, which file tools can no longer reach.
- Checkpoint contents are protected by location only (no signature).
- Files with several hard links are refused even if harmless.
- Passing tests do not make the system secure.
