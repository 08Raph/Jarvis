# Prompt 7: coding, research, documents, plugins and skills

**Not run on Windows. Not run with the real Qwen model. No real internet search was run.** This is not a security approval, not a production-readiness
statement and not a Windows or real-model verification. Prompt 6 is still not Windows-verified; real keyboard/mouse automation and consequential desktop
actions stay disabled by default until the Windows checklist (sections 6e-6g) is done. These tools add to the local model; none of them is, or
replaces, the model. The from-scratch model does not exist yet, and memory updates do not retrain any weights.

## 1. What exists now (all through the existing validated registry and executor)
Same path as every desktop tool: schema validation -> platform -> risk -> policy (category switch) -> pin/bind -> confirmation -> re-bind -> audit -> run with
timeout and emergency stop. New permission categories are all OFF by default: `code_read`, `code_edit`, `code_run`, `research_online`, `plugins`.
Changing a project, running code, running a plugin and every web request are **always** asked, whatever the category switch says.

| Group | Tools | Online? | Notes |
|---|---|---|---|
| Inspect code | `repo_overview` `read_lines` `search_code` `analyze_error` `git_info` `diff_snapshot` `verify_fix` | no | project folders only (`jarvis desktop allow-project`); secrets files (.env, keys) never read; search is plain text; git runs with hardened flags |
| Change code | `edit_code` `write_code` `undo_code_edit` `snapshot_project` | no | diff shown in the question, backup before every change, undo refuses if the file changed since, Python syntax warning |
| Run code | `run_tests` `run_code` | no | **sandbox only**; refused when there is no genuine sandbox |
| Documents | `read_document` `list_sources` `verify_quote` `cite_sources` `compare_sources` | no | text/code, .html, .docx (stdlib), .pdf (optional `pypdf`) |
| Web | `web_fetch` `web_search` | **yes** | needs the `web_research` internet feature AND your approval of each request |
| Plugins | `plugin_<name>` | no | only after review + explicit activation, only in the sandbox |
| Skills | CLI only (`jarvis skill ...`) | no | saved, reviewed, versioned task lists |

Installing, reviewing, activating, rolling back plugins/skills, and changing settings are **command-line actions for you**; they are not tools, so no model reply can call them.

## 2. The sandbox (Linux only) and its honest limits
`dev/sandbox.py` starts the program inside new user, mount, pid and network namespaces with a private root (read-only system directories, a throw-away copy
of the files at `/work`, a size-limited `/tmp`), no capabilities, `no_new_privs`, CPU/memory/file/process limits, a wall-clock timeout, an output cap, a
disk-growth watchdog and a stop callback; the whole process tree dies with it. Before first use it **tests itself** (a host file, /root, /home, /etc/passwd,
/var invisible; network connect fails; /usr not writable). If any step fails, the sandbox is reported unavailable with the reason.
- **Windows and macOS: no sandbox exists. `run_tests`, `run_code` and plugins are refused there.** Offline analysis (reading, searching, `analyze_error` on
  pasted output, editing with backups) still works. Nothing pretends otherwise.
- Not provided: seccomp filtering, cgroup limits (CPU/memory use rlimits), protection against kernel bugs (the kernel is shared), a hardware boundary. Treat it as
  containment against mistakes and hostile snippets, not as a proof of safety.
- Test runs use a copy of the project; nothing is written back. Dependencies must already exist in the interpreter's environment; Jarvis never installs packages.

## 3. Verification rules
- A test run is "verified" only when the runner's own printed summary is parsed **and** agrees with the exit code. A missing or contradictory summary is "not confirmed".
- A killed run (timeout/stop/limit) is UNKNOWN, never a pass.
- `verify_fix` says VERIFIED FIX only if a recorded run of the same tests failed, a later one passed, and non-test source files changed in between. "Passing" alone, "only tests changed", "no change" and "no runs" each get their own answer.
- `run_code` reports that the program ran (exit code), explicitly not that its output is right. A plugin's answer is its own claim and is never marked verified.

## 4. Research and documents
- Every document or page read becomes a **source** (id, location, time read, SHA-256, text) in the protected profile folder. The source list comes from those records, not from the model. `verify_quote` checks a quote against the stored text. An unreadable document (encrypted, scanned, damaged, unsupported, too large, library missing) is reported as unreadable and never summarised.
- Text from files, pages, repositories and logs is untrusted: shown to the model only inside `<untrusted_data>`, and no tool takes an instruction from it.
- Private documents stay local. The only things that can leave are the URL or search words you approve; they are refused if they contain a secret or repeat a 6-word passage of a document Jarvis has read. Limit: this guard sees documents read through `read_document`, not every file on the PC, and a determined paraphrase is not caught.
- Online is separate and off by default (`jarvis net enable web_research`). Only https, public hosts (private/loopback/link-local addresses are refused, also after redirects), standard port, no user/password in the address. Limit: DNS is resolved once to check and again to connect, so DNS rebinding is narrowed, not closed.
- Search providers: Wikipedia, or **your own** SearXNG instance (`jarvis research set-searxng https://...`). There is no bundled general web search. Search results are leads; a page must be fetched to become a source.
- `compare_sources` is a heuristic over numbers in sentences containing a topic. It points at possible disagreement; it does not decide who is right and does not prove agreement.

## 5. Plugins and skills
Plugin = manifest + Python file. Declared: name, version, inputs, outputs, license, dependencies, tests, permissions wanted. The only grantable permission is `compute`; wanting files, network, input or system access is a review **blocker**. Review (static) reports license, dependency pinning/installation, imports of network/process/dynamic-code modules, risky calls and references to protected names. Activation is a command you run with the code hash you reviewed; any change to the code afterwards stops the plugin from loading. Declared tests run in the sandbox at activation; test status shown is derived only from recorded runs ("UNTESTED", "STALE", or "n/m passed"). Versions are kept; a new version is never activated automatically; rollback returns to an earlier version only if its code still matches what was approved. Review is not proof of safety: the sandbox is the boundary.
Skills are saved task lists (versioned, reviewed by hash, activated, rollback). They call ordinary tools through the executor, so every step is still checked and confirmed. Their status counts only recorded runs in which every step was verified by its own check.

## 6. Tests (this is what was actually executed, on Linux, with fakes where labelled)
Command: `cd project-jarvis && PYTHONPATH=src python -I -m unittest discover -s tests`
Stage D baseline 621 OK (2 skipped) -> **811 OK (2 skipped)**; no earlier assertion was changed.
| File | Tests | Kind |
|---|---|---|
| `test_p7_sandbox.py` | 19 | REAL jail (host-file/network isolation, timeout, cancel, output/disk/memory limits); MOCKED unavailability |
| `test_p7_code_tools.py` | 52 | REAL files, executor, git, sandbox runs; FAKE MODEL agent cases; MOCKED no-sandbox |
| `test_p7_research.py` | 45 | REAL files/docx/pdf; **MOCKED internet and DNS**; FAKE MODEL |
| `test_p7_plugins.py` | 60 | REAL plugin review/activation/sandbox runs; skills on the simulated desktop |
| `test_p7_regression.py` | 14 | REAL; frozen Stage D tool list; extension rules |
Negative controls (one safeguard removed in a scratch copy, tests must fail): public-host check, document-overlap guard, plugin code-hash recheck, sandbox refusal, tests-only rule in `verify_fix`, always-confirm rule: each made tests fail.
The 2 skipped tests are unchanged from Stage D (the opt-in real-model test, and the PyTorch parameter-formula test because PyTorch is not installed here).

## 7. Not done / open
- Nothing here ran on Windows; there is no sandbox on Windows, so code execution and plugins are unavailable there until a Windows isolation design exists and is tested.
- The real Qwen model has not been tested with these tools; the tool catalogue is longer, which may cost small-model reliability. Unknown.
- No real internet, Wikipedia or SearXNG request was made; parsing is tested against hand-written replies.
- `search_code`/`read_lines` read files directly; `git_info` is the only external program run outside the sandbox (fixed read-only sub-commands).
- Plugin dependencies installed in a virtualenv outside system directories may not be visible inside the sandbox; the run then fails with a "dependency missing" report.
- Plugin/skill management by CLI has had light testing (in-process); no interactive terminal session was exercised.
- Source-guard for outgoing text covers only documents read through `read_document`.
- Sandbox: no seccomp/cgroups, shared kernel.
