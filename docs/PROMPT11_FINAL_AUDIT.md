# Prompt 11: final integration, security audit and acceptance

**Recommendation: NOT a release candidate. Preview only, for people who will run the Windows checklist and report back.**
Everything here was checked on Linux with scripted/fake models. Nothing was run on Windows, with Qt, with real Qwen weights, with audio hardware or
with a real game. The audit is a code review plus probes of the code that exists; it is **not comprehensive** and is not a security approval.

Qwen is a pretrained interim engine. Nothing here claims it was trained from scratch.

## 1. Baseline
- Input: `project-jarvis-p15-prompt10.zip` (737,622 bytes, SHA-256 `06c9aab8...a064f53`). Extracted into a clean directory, checkpoint copy kept, original archive untouched.
- Baseline suite: 1230 tests, OK, 6 skipped (Prompt 10 claim reproduced).
- Earlier completion claims were re-checked where cheap (see 5); none was taken on trust for the security-relevant paths.

## 2. Architecture and integration map (as found in the code)
| Link | Finding |
|---|---|
| Model -> tools | `agent.py` accepts exactly one `<tool_call>` per reply; tool output returns inside `<untrusted_data>` after `neutralise()` (closing tags, `tool_call` tags and zero-width variants are rewritten). The model never calls a handler directly: only `executor.py` runs `ToolSpec` handlers (grep: no other caller outside the unused legacy `tools/registry.py`). |
| Permissions | One place: `executor.py` validates -> binds/pins paths -> policy (`desktop/policy.py` categories; modes only HIDE tools) -> fingerprint-bound confirmation -> re-bind check -> audit -> run in a worker. GUI and CLI both go through `AppController` / `Executor`; the window has no switches that grant permissions. |
| Missions / schedules | Use the same `Executor` specs (`runner.ex.specs`). Approval is single-use and bound to `plan_hash`; edited steps flag `tampered`; schedules need their own hash-bound, expiring, run-limited approval, and refuse code-run / plugin / online steps; occurrences are claimed before running (no double run after a restart). |
| Network | `net/policy.py` is the only network module. Every feature is off by default, https only, hosts must resolve to public addresses, redirects are re-validated. (Hardened here: F-1.) |
| Sandbox | `dev/sandbox.py` (Linux namespaces + pivot_root + rlimits, self-tested). Linux only: on Windows code running and plugins refuse. |
| Config / data | Everything user-owned lives under `cfg.profile_dir`; paths.py treats it (and the shared data root of a named profile) as protected from all file tools. One resolver (`config.Config.profile_dir`). |
| Shutdown | `AppController.shutdown` closes the assistant only when no worker is still running (Prompt 10); the lock file is released; voice is not exposed in the window. Window code itself was never run. |
| Optional dependencies | llama-cpp, PySide6, sounddevice, tesseract are each imported lazily with a plain error; absent ones do not break unrelated commands (checked here: none of them is installed and the suite passes). |

Dead or duplicate code: `tools/registry.py` (`ToolRegistry`, `open_app`, `safe_eval`) is used only by tests, not by the application. Left in place (removing it would
delete tested behaviour and gains no safety); a future cleanup should delete it together with its tests. No other duplicate permission path was found.

## 3. Findings
Severity is my judgement of impact on a single-user, local-first desktop assistant.

| ID | Sev | Component | Finding and evidence | Status |
|---|---|---|---|---|
| F-1 | High | `net/policy.py` | The public-host check judged only the literal address, so an AAAA answer such as `64:ff9b::7f00:1` (NAT64 for 127.0.0.1), `::ffff:10.0.0.1` or a 6to4 address wrapping a private IPv4 passed it. Probe: those addresses were accepted before the fix. | Fixed: embedded IPv4 is extracted and judged; local-use NAT64 refused. |
| F-1b | High | `net/policy.py` | DNS rebinding: the name was resolved and checked, then the HTTP client resolved it again and could connect elsewhere. | Fixed: after connecting, the real peer address is checked and the response is closed on failure (fails closed if the peer cannot be read; skipped only when a proxy is configured, because the peer is then the proxy). Residual: with a proxy the proxy's own resolution is trusted. |
| F-2 | Medium | `memory/secrets.py` | Redaction missed quoted passwords with spaces, env-style names (`DB_PASSWORD=`), URL credentials, `Authorization:` headers, `Bearer` tokens, long `*token=` values. These flow into memory, mission records and diagnostics. | Fixed (one pattern file; all three consumers use it). Best-effort by nature: unusual secrets can still be missed. |
| F-3 | Medium | `memory/store.py`, CLI | A damaged memory database raised a raw traceback. The file was not modified, but the user got no guidance. | Fixed: `MemoryDatabaseError` with a plain message pointing to `jarvis restore`; file is never changed or deleted. |
| F-4 | Medium | `memory/cli_memory.py`, docs | `forget-all` left copies of what Jarvis had read or run (`research_sources`, `code_snapshots`, `code_runs`) and the docs did not say so. | Fixed: derived copies are removed; undo points (`desktop_checkpoints`, `code_backups`) are kept unless `--include-undo-points`, and the command says what it kept. Documented in `PRIVACY_AND_NETWORK.md`. |
| F-5 | Medium (regression I introduced, caught by the full suite) | `memory/secrets.py` | The first F-2 patch put the literal word the existing "no API-key logic in source" guard forbids into the source. | Fixed before packaging; guard unchanged. |
| R-1 | Info | `desktop/ocr.py` | The bundled OCR script is run with `-ExecutionPolicy Bypass`. This only lets Jarvis' own shipped `.ps1` run; it is not a UAC or OS-protection bypass. Windows only, unverified. | Accepted; documented. |
| R-2 | Low | `missions/schedule.py`, approvals in general | Approvals, plan hashes and permission files are plain files owned by the user. Anything that can already write to the user's profile folder (another program running as the user) can forge them. The hash defends against accidental edits and against the model/UI, not against a hostile local process. | Not fixable without OS-level protection or signing keys; documented as a limitation. |
| R-3 | Low | `memory/secrets.py` | "The secret: to good bread..." style sentences are redacted (over-redaction, safe direction). | Accepted. |
| R-4 | Low | `missions` | A corrupt `active.json` shows a raw message in the CLI (the window status is actionable). | Not changed. |
| R-5 | Medium (unverified) | Windows backends | All reparse-point/junction handling, UIAutomation, input injection, OCR and emergency stop are written against Windows APIs that were never run. Linux tests simulate `lstat` results only. | Open; needs the Windows checklist. |
| R-6 | Medium (unverified) | Sandbox on Windows | No sandbox exists on Windows: code running and plugins refuse by design. Linux sandbox cannot limit process count when run as root (documented). | Open by design. |
| R-7 | Medium | Real model | Prompt-injection and malformed-output tests use scripted models. Behaviour of real Qwen under injection is unmeasured. The control that matters (central policy + confirmation) does not depend on the model, but that is only shown with scripted replies. | Open. |

No finding was found that lets a model, plugin, skill, mission or schedule grant itself a permission. No path was found that runs a tool handler outside `Executor`.
What was **not** examined deeply: `dev/plugins.py` and skills beyond their hash/state handling, `capture_guard` and vision internals beyond their tests, every Windows-only branch, the PySide6 window code,
the voice stack, third-party dependency integrity, and the installer scripts (reviewed by reading only).

## 4. Regression tests and negative controls
New: `tests/test_p11_security.py` (20 tests: F-1, F-1b with a real loopback server, F-2, F-3, F-4). Command:
`cd project-jarvis && PYTHONPATH=src python -I -m unittest discover -s tests -p test_p11_security.py`

Negative controls (disposable copies of the tree; the original file put back or the safeguard disabled; result = tests that fail):
| Removed safeguard (disposable copy) | Tests that failed |
|---|---|
| F-1: pre-fix `net/policy.py` | 5 of 20 (`F1`, `F1b` embedded-address, rebinding, peer-check tests) |
| F-2: pre-fix `memory/secrets.py` | 4 of 20 |
| F-3: pre-fix `memory/store.py` + `cli.py` | 3 of 20 |
| F-4: pre-fix `memory/cli_memory.py` | 3 of 20 |
| Unmodified tree (control) | 0 of 20 |
| Existing safeguard: `SafePaths` link/junction/reparse refusal disabled | 5 existing tests (symlink, Windows symlink/junction/unknown reparse tag simulations, loop) |
| Existing safeguard: schedule approval hash comparison disabled | `test_mission_edited_after_schedule_approval_stops_runs` |
| Existing safeguard: `assert_public_host` made a no-op | 4 tests (2 new, plus `test_MOCKED_a_redirect_to_a_private_address_is_refused` and `test_MOCKED_private_and_loopback_hosts_are_refused_by_the_real_policy`) |

Note: the first two existing-safeguard runs were made before F-5 was fixed, so they also show `test_no_api_key_or_quota_logic_in_source` failing; that is the regression the full suite caught. Not every safeguard was given a negative control (confirmation binding, sandbox isolation and plugin hashing were reviewed and covered by existing tests, but I did not mutate them).

## 5. Acceptance matrix, journeys
| Journey | Status | Evidence / what is missing |
|---|---|---|
| A Local assistant | BLOCKED | No Qwen GGUF and no `llama_cpp` here. Verified with a fake engine: missing model, missing runtime and corrupt file give clear errors and no reply text. Real inference never run. No external service is contacted by chat (network features are off by default and tested). |
| B Memory | PARTIAL | Add / read / correct / persist across reopen / export / delete / secret refusal: automated tests pass (Linux). Window page not run. Deletion and export match `MEMORY.md` after F-4. |
| C Desktop operation | BLOCKED | Windows only. Policy, path, confirmation-binding, cancel and STOP logic are tested with fakes. Real window targeting, input and emergency stop on Windows: NOT TESTED. |
| D Coding and research | PARTIAL | Linux sandbox self-test and escape probes pass; backups/diffs/tests run in fixtures; online research uses mocked HTTP. Windows: sandbox unavailable by design. No real internet request was made. |
| E Vision and gaming | BLOCKED | No screen, no Tesseract, no GPU, no real PresentMon log. Parsers, capture guard and "unavailable" reporting tested on fixtures. |
| F Missions and scheduling | PARTIAL | Plan / approve / run / cancel / pause / crash recovery / tamper / duplicate-claim / schedule approval limits: tested with the real Executor and fakes. No real tool side effects on Windows. |
| G GUI and packaging | BLOCKED | No PySide6, no Windows, no PyInstaller build, no installer. Only packaging manifests and the asset checker were tested. |

## 6. Release acceptance decision
| Area | Status | Reason |
|---|---|---|
| 1 Source and automated-test quality | PARTIAL | Full suite from the working tree: 1250 tests, OK, 6 skipped (1230 existing + 20 new). The 6 skips are opt-in real-model/Windows/hardware tests. A Linux pass is not evidence for Windows. Tests are largely scripted. |
| 2 Security and permission enforcement | PARTIAL | Central enforcement reviewed and probed on Linux; five defects fixed; Windows paths and real-model injection unverified; not comprehensive. |
| 3 Real local-model functionality | BLOCKED | No weights/runtime. |
| 4 Voice | BLOCKED | No audio device or PortAudio; only fakes tested. |
| 5 Windows desktop operation | NOT TESTED | No Windows. |
| 6 GUI behaviour | NOT TESTED | No PySide6; window code never run. |
| 7 Offline and privacy | PARTIAL | Network policy, off-by-default features, redaction, deletion and corrupt-file behaviour verified by tests and probes. "Works fully offline" was not exercised end to end (no real model), so it is not claimed. |
| 8 Windows packaging and installer | NOT TESTED | Scripts reviewed only. |
| 9 Clean-machine install | NOT TESTED | |
| 10 Overall release readiness | NOT READY | Items 3, 5, 6, 8, 9 have no evidence. |

## 7. Offline and privacy status
Network-capable code: `net/policy.py` (the only socket user, verified by `test_offline_guarantees`), model downloads (explicit `jarvis models fetch`, through `NetworkPolicy` with `model_download` enabled; the SHA-256 comes from the host's own metadata when it supplies one, so it detects corruption, not a malicious host), research (`research_online` category, explicit consent per feature).
No telemetry, no cloud fallback (a test scans the source for API-key/quota logic and for forbidden vendor names). Stored copies and their deletion are tabled in `PRIVACY_AND_NETWORK.md`.
Corrupt memory, mission, schedule, settings and personality files fail safely without being overwritten (probes + tests).

## 8. Real-model status
Not run: no weights, no runtime. Needed: Windows 11, the Qwen GGUF, `llama-cpp-python`, then the opt-in tests (`JARVIS_REAL_MODEL_TEST=1`) and checklist 6k.7.
Scripted-model tests are labelled FAKE_MODEL in their names and prove only the surrounding code.

## 9. Windows build and GUI status
No build, no installer, no window was run. See `WINDOWS_TEST_CHECKLIST.md` section 6l for what to run and record.

## 10. Blockers and manual tests
1. Windows 11 with Python 3.12+, PySide6: build, `--selftest`, every page, dialogs, DPI, dark mode.
2. Real Qwen GGUF: inference, tool selection, injection and malformed-output behaviour, cancellation, memory use.
3. Windows desktop backend: junction/reparse handling, window targeting, input, emergency stop, UAC prompts left alone.
4. Voice with a real microphone and speakers.
5. Installer, upgrade, uninstall, clean VM, SmartScreen.
6. Independent security review of the Windows-specific code and of the dependencies' provenance.

## 11. Commands used
- Suite: `cd project-jarvis && PYTHONPATH=src python -I -m unittest discover -s tests`
- Fresh-extraction run of the final archive: recorded in section 12.

## 12. Final archive
`project-jarvis-final-candidate.zip` (name kept as the task required, although the recommendation is *not a release candidate*). Its size and SHA-256 cannot be written inside itself; they are in the delivery message. Fresh extraction into an empty directory, then `PYTHONPATH=src python -I -m unittest discover -s tests`: 1250 tests, OK, 6 skipped, no failures. Earlier checkpoints were not touched.
