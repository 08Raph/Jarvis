# Adult-mode restriction: security audit (starting commit df60275)

Scope: is the temporary restriction of Savage, Flirty, Sexy and Romantic genuinely enforced in the code, not merely covered by green tests? Evidence below is **automated tests of the real application code**
(Persona, AppController, LocalAssistant, cli, audit/suite/selfcheck) with a **scripted fake model** and **no Qt**. Nothing was run with a real model, PySide6, Windows or audio hardware.

## Preserving a preference is not authorizing a mode
A saved choice (`style: savage`, the adult opt-in, the intensities) is the user's **record of what they wanted**. Loading it keeps that record (`saved_adult_style`, opt-in and intensities) but the live
style is Normal. Authorization is a separate, code-level fact: `styles.ADULT_MODES_RESTRICTED`. A file, an opt-in, a chat message or a UI state can express a wish; none can grant the mode. If a reviewed commit
lifts the constant, a remembered choice returns only when the opt-in is still `true`. Loading never rewrites the file (tested: bytes and mtime unchanged).

## Selection paths and where the restriction is enforced
| Path | Where enforced | Restricted value encountered |
|---|---|---|
| UI combo / Apply (`window._persona_apply`) -> `controller.persona_set` | `Persona.set_style` -> `_apply` -> `PersonaSettings.validate(restricted=...)` | ValueError with the reason, shown in the status bar; nothing saved |
| UI opt-in checkbox -> `controller.persona_adult` | opt-in is stored but does not unlock; message says so | style stays Normal |
| Controller `persona_option`, `persona_level`, `persona_control`, `persona_reset`, `persona_normal` | all go through `_apply` (validate) | cannot carry a `style` argument past validate (tested) |
| Load at startup (`Persona._load`, `_recover_fields`) | parse with `restricted=False`, then `_gate()` | live style Normal; preference remembered; wrong types/unknown ids fail closed |
| Chat text, voice text, prompt injection | no code path from message text to `set_style`; `observe()` can only lower (minor cue, stop, distress) | file bytes unchanged (tested with 8 hostile messages) |
| Defence in depth, `observe()` | an adult style found selected while restricted is answered in Normal with a notice | tested by forcing `settings` in memory |
| CLI `persona set`, `persona adult`, `option`, `control`, `reset`, `normal` | same `Persona` methods | `set` exits 2 with the reason |
| CLI `persona audit/suite/selfcheck` | default skips the four; naming one without the flag exits 2 | `--include-restricted-modes` only on these three (parser tested) |
| Reset | `Persona.reset()` returns plain defaults, clearing the remembered choice | tested |
| Configuration import / backup | none exists for `persona.json`; a replaced file is just another load | covered by the load rows |
| `voice_by_persona` (voice manager) | stores a **voice id** per style id; never selects a personality | tested |
| Direct code: `persona.settings = ...` | not reachable from UI/CLI/chat; prompt composition still falls back to Normal in `observe()` | tested |

## Review-only unlock: what was found and fixed
Finding (confirmed defect, fixed): the first implementation was a plain mutable attribute, `persona.review_unlock = True/False`. It was **not exception-safe** (`selfcheck` re-locked only on success), **not nest-safe** (an inner run could re-lock an outer one), visible to **other threads** sharing the object, and **settable by any code**. It could also have allowed a lifted style to be **written to `persona.json`** if a review ran on a profile-backed persona.
Now: `Persona.review_scope(enabled)` is a nestable context manager bound to the entering thread; `review_unlock` is a read-only property; `_save()` writes nothing while a scope is active. audit, suite and selfcheck use `with`/`ExitStack`, so the lock returns however the run ends (model failure, Ctrl-C, SystemExit, exception).
Verified by tests: exception/KeyboardInterrupt/SystemExit restore; nesting; a second thread never sees the unlock; two threads' scopes do not end each other; a normal controller/assistant created during a review is restricted and its file untouched; a failing report write cannot leave the lock open (the run has already ended); no disk write during a review; the flag/ environment variables/config keys are inert; the flag is accepted only by the three review commands. A source scan keeps `review_scope`, `review_unlock`, `ADULT_MODES_RESTRICTED` and `restricted=False` out of the UI, controller and every other module.

## Test helper `tests/restriction_lifted.py`
Test-only (no file under `src/` mentions it; tested). Nine older modules import `setUpModule/tearDownModule` from it to exercise the dormant adult definitions; `tearDownModule` always restores the constant. The new restriction modules (`test_p55`, `test_p56`, `test_p57`) never import it, assert the restriction is on at start, and a subprocess test runs them before and after a lifted module in both orders.

## Remaining limitations
- The enforcement is a module constant plus code paths. Anyone who edits the source or runs Python against the objects can change it; this is a product restriction, not protection against a local developer.
- `Persona.preview(style)` still returns the exact prompt text for a restricted style (read-only, selects nothing).
- Real-model behaviour of the four modes is unreviewed; the restriction exists because of that.
- The distress detector is a small keyword heuristic (see runbook section 8); the restriction does not make it comprehensive. No detector pattern or frozen dataset was changed in this audit.
- Qt rendering of the restriction note, Windows behaviour and audio were not run.

## Post-fix review (starting commit dd0d541)
- Independent re-inspection of `review_scope()` found no new bypass: callers are limited to audit, suite and selfcheck (source scan), the property is read-only, scopes are thread-bound and nest, nothing is saved while one is active.
- **Confirmed gap, fixed:** `Persona.preview()` returned the full "exact instructions the model receives" for a restricted style, which both published the prompt of an unreviewed mode and was misleading (the model cannot receive it). A restricted style is now described with its reason and an empty `model_instructions`; the window shows "Temporarily unavailable". Tests: `test_p58`.
- **Report honesty added:** every audit and suite report carries `evidence_status` (simulated / real incomplete / real complete, always "automated, not human-reviewed, applies only to the identified model", and a note when cases were not run) and `human_reviewed: false`; the CLI prints it; the model-identity block (including the runtime's own error text) has file-system paths removed.
- Remaining risk unchanged: a developer who edits the source or the constant can lift the restriction; real-model behaviour of the four modes is unreviewed.
