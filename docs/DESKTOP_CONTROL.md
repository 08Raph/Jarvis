# Desktop control: Jarvis operating your own Windows PC

## Honest status
| Piece | State |
|---|---|
| Permission policy, confirmations, argument validation, path containment, audit log, emergency stop | Implemented, **tested** (Linux) |
| Executor in a separate worker process with timeouts and kill-on-stop | Implemented, **tested** (Linux); the Windows worker path (`jarvis.exe _desktop-worker`) is **not run** |
| File tools (search, read, create folder, write, edit, copy, move, rename, recoverable delete, undo) | Implemented, **tested on real files** (Linux filesystem; NTFS behaviour such as locks/long paths **untested**) |
| System information (CPU, RAM, disks, network state, GPU) | Implemented; Linux sources **tested**; Windows sources (GetSystemTimes, GlobalMemoryStatusEx, InternetGetConnectedState, nvidia-smi, Win32_VideoController) **not run** |
| Act-observe-verify task runner (preview, checks, retries, rollback, cancel, history) | Implemented, **tested** |
| Model-driven agent (`jarvis agent`) | Loop **tested with a scripted FAKE model only (no real model has been run; see `docs/AGENT_INTEGRATION_REVIEW.md`)**. Whether Qwen3-1.7B produces useful tool calls is **unknown** - a 1.7B model may be weak at multi-step tool use |
| App discovery (Start menu .lnk, Store apps via Get-StartApps) and launch with window check | Written; **not run on Windows**. Launching an allow-listed program with a process check is **tested on Linux** |
| Window list/focus, SendInput keyboard/mouse, screenshots (GDI), UI Automation (comtypes), Windows OCR | Written against documented APIs; **never executed** - all UI tests use the clearly FAKE `SimulatedDesktop` |

Nothing here should be described as working on Windows until the checklist (docs/WINDOWS_TEST_CHECKLIST.md, section 6d) has been run.

## How it is built (separation)
```
you / local model ─► agent.py: accepts ONE JSON tool call per turn; never runs code
                  ─► task.py: act → observe → verify, preview, cancel, checkpoints, history
                  ─► executor.py: validate → least privilege → permission → confirm → audit → run with timeout
                  ─► tools.py: a FIXED list of 27 tools  ─► backend (Windows APIs, or the FAKE simulator)
```
There is **no** tool for running commands, PowerShell, scripts or arbitrary programs, editing the registry, changing system or
security settings, or sending anything off the PC. Jarvis cannot write program or script files (.exe .bat .ps1 .vbs .lnk .py .msi ...),
so the model cannot create code and get it run. The only fixed PowerShell use is Jarvis' own OCR script, `Get-StartApps` and the GPU-name
query, with no user or model text in the command.

## Permissions (per profile, all OFF except `system`)
`jarvis desktop status` shows them. Turn on only what you need:

| Category | Allows | Turn on |
|---|---|---|
| system | CPU/RAM/disk/network/GPU readings | on by default |
| files_read / files_write | read / change files **inside folders you allow** | `jarvis desktop enable files_read files_write` + `jarvis desktop allow-folder "C:\Users\you\Documents\JarvisWork"` |
| apps | find and start installed programs | `jarvis desktop enable apps` |
| windows | list and switch windows | `jarvis desktop enable windows` |
| ui | read and operate buttons/menus/text boxes via accessibility | `jarvis desktop enable ui` |
| input | keystrokes and mouse clicks | `jarvis desktop enable input` |
| screen | screenshots and OCR (kept on this PC, last 20) | `jarvis desktop enable screen` |

Allowed folders can never be a drive root, your whole user folder, Windows, Program Files or ProgramData. Paths are checked after following
links/junctions; `..`, alternate data streams (`file:stream`), device names (`con`), UNC/`\\?\` paths are refused. Guest mode (`--guest`) only allows `system`.

## When Jarvis asks you first (type `yes`)
- **Always:** overwriting/replacing a file, deleting (to Jarvis' own recycle folder), installers, Alt+F4 / Ctrl+W / Delete keys, buttons named
  like Delete/Remove/Send/Pay/Buy/Submit/Uninstall.
- **Starting a program**, unless you trusted it: `jarvis desktop trust-app ID` (installers still ask).
- **Keystrokes, clicks and UI actions:** every time by default; `jarvis desktop input-confirm once_per_task` asks once per window per task.
- **In agent mode, after the model has seen any outside text** (a file, window titles, UI text, OCR): every further change asks.
- Never asked, simply refused: administrator actions, UAC prompts, Windows Security/Defender, Task Manager, Registry Editor, sign-in and
  password-manager windows, password fields, command prompts/PowerShell/terminals/script hosts and the Run dialog (typing there would
  mean running arbitrary commands), the Windows key, Ctrl+Alt+Del, Ctrl+Shift+Esc, and **any input or UI action while an anti-cheat
  program (EasyAntiCheat, BattlEye, Vanguard, FACEIT, ...) is running**.
- **Least privilege:** if Jarvis itself runs as administrator it refuses every change and only observes. Start it normally.

## Act, observe, verify
Each tool checks its own result after acting (file read back and hashed, window looked for, value read back, foreground window checked).
Results are one of: VERIFIED (a check confirmed it), UNVERIFIED (the action was sent but nothing could confirm it, e.g. a key press),
NOT DONE / failed, denied, declined, timeout (result UNKNOWN), stopped. Jarvis' final report is built from these statuses, not from what the model says.

Task files add your own checks:
```json
{"goal": "add a line to my notes and check it",
 "steps": [
  {"tool": "edit_file", "args": {"path": "notes.txt", "old_text": "TODO", "new_text": "TODO\n- call Sam"},
   "checks": [{"tool": "read_file", "args": {"path": "notes.txt"}, "field": "untrusted_text", "op": "contains", "value": "call Sam"}],
   "on_fail": "rollback"}]}
```
`jarvis desktop task notes.json --preview` shows what each step needs without doing anything; drop `--preview` to run it.

## Commands
    jarvis desktop status | tools [--json] | enable CAT... | disable CAT... | allow-folder DIR | remove-folder DIR
    jarvis desktop trust-app ID | untrust-app ID | input-confirm every_action|once_per_task
    jarvis desktop run TOOL --args '{"...": ...}'      one step, same checks and confirmations
    jarvis desktop task FILE.json [--preview]          act-observe-verify task
    jarvis desktop history | audit | checkpoints | undo ID
    jarvis desktop stop | clear-stop                   emergency stop from another window
    jarvis agent "open notepad and type hello"         local model chooses steps; you approve
    add --simulate after `desktop` (or to `agent`) to use the FAKE simulated desktop: safe to try, touches nothing

## Emergency stop
Ctrl+C in the Jarvis window, `jarvis desktop stop` from another window (creates a STOP file that also stops worker processes), or the global
hotkey **Ctrl+Alt+Pause** (Windows; registered with RegisterHotKey, untested). The running tool is interrupted (the worker process is killed);
its result is then reported as unknown. New tasks refuse to start until `jarvis desktop clear-stop`.

## Untrusted content
Text from files, window titles, UI elements, OCR or apps is given to the model inside `<untrusted_data>` with `<` and `>` neutralised, so it cannot
close the tag or forge a tool call; instruction-like phrases are flagged. It never changes the policy: permissions and confirmations are enforced
by the executor regardless of what the model asks.

## What is stored (this PC only, per profile)
`desktop_permissions.json`, `desktop_audit.jsonl` (every attempt; typed text and file contents are stored only as length + hash; secrets
redacted best-effort), `desktop_tasks.jsonl` (history), `screenshots\` (last 20), `app_index.json`, and `desktop_checkpoints\` (backups/undo
points, which DO contain copies of your files). `jarvis memory forget-all` deletes all of these except the checkpoints, which it names.

## Known limits
- Some ordinary apps can themselves run things (Explorer's address bar, a browser's URL bar, Office macros). Jarvis cannot recognise every such
  surface. That is why every keystroke/UI action asks by default and shows the exact text; read it before typing `yes`.
- Windows may refuse to bring a window to the front (foreground lock); Jarvis then reports it and does not type.
- UIPI: Jarvis (normal user) cannot send input to elevated windows; that is reported, not worked around.
- Apps without accessibility support (many games, some Electron/Java apps) expose little to UI Automation; coordinate clicks are a fallback and are
  only UNVERIFIED until an observation confirms the effect.
- OCR needs an OCR language installed in Windows; it can misread. GPU metrics are only available for NVIDIA GPUs with `nvidia-smi`.
- comtypes in a PyInstaller build generates its COM wrappers at first use; whether that works in the frozen app is untested.

## Security hardening pass
A limited hardening pass (Jarvis's own data protected from file tools, confirmations bound to the exact action, strict tool-call parsing, truthful reporting) is described in `docs/DESKTOP_HARDENING.md`, including what it does **not** cover. It has not been run on Windows.
