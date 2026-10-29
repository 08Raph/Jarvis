# Requirements-based acceptance report (commit c508666, 2026-10-29)

**Source of the requirements (updated 2026-10-29).** The owner's prompt pack was found in the session's attachment folder: 73 files from Prompt 6 (2026-10-25) to the current instruction, indexed in `PROMPT_PACK_INDEX.md`. The baseline requirements document is `MASTER ENGINEERING PROMPT 19` (product vision, hardware tiers, 15-category scorecard, the 12 hands-free rules, testing rules). **Prompts 1 to 5 were not available and were not checked**; a requirement that exists only there is not covered by this report. The rows below were first written from the repository's audits and the owner's stated requirements, then cross-checked against Prompts 17, 19 and the approved-UI prompt.

**Speaker recognition ("only respond to my voice") is DECIDED: not required (owner, 2026-10-29). It is out of scope and is not implemented; no voice biometrics will be built.** A search of all 73 prompts for speaker/voiceprint/voice-ID/"my voice"/"only respond"/enroll found no such requirement (the hits are about speaker output, speaker mute and Kokoro voice IDs). It appears only in the owner's earlier project notes for an older Jarvis. Whether it is an original requirement is for the owner to confirm; no substitute was built.

**Status words.** *Verified (real)* = ran for real on Windows CI with a real model/UI, at the commit named. *Verified (fakes)* = unit-tested with fake runtime, fake audio or a simulated desktop. *Implemented* = code exists with tests but no run beyond that. *Missing* = no implementation found. *Blocked* = needs something this project cannot supply. Evidence for "real" is in `VERIFICATION_LEDGER.md`; "older commit" means bee5873 (2026-10-27), 68 commits and about 10,700 source lines before this one.

| # | Requirement | Where | Status | Evidence and what is NOT shown |
|---|---|---|---|---|
| 1 | Local AI, no paid API, no mandatory cloud | `inference/`, `models/`, `brains/`, `net/policy.py` | Verified (real, older commit); current code unverified | Qwen3-1.7B loaded and answered on a Windows CI runner (runs 40422256861, 40431278616), also with the app's network firewall-blocked. A source scan test forbids vendor API/quota logic. Not run on the current code or a person's PC. |
| 2 | Target model Qwen3-8B Q4_K_M | `models/catalog.py` | Partial: **decision needed** | The 8B entry exists and is installable by choosing it, but the first-run wizard never selects it (`auto_recommend=False`) and it was never benchmarked. On a 32 GB / RTX 4070 SUPER class machine the wizard still proposes what the selector picks among auto-recommended models. Changing that is a product decision, not a defect fix. |
| 3 | Guided setup and System & Model Readiness UI (no PowerShell for customers) | `app/setup_engine.py`, `app/readiness.py`, `ui/` | Verified (real, older commit) | Real clicks drove consent, download with progress, integrity check, load, first answer, cancel and close mid-download, damaged-model refusal. Looks, scaling and accessibility not shown. |
| 4 | Model integrity: SHA-256, damaged file refused, rollback | `models/manager.py`, `models/package.py` | Verified (real, older commit) | A one-byte change was detected and the model re-fetched (run 40431278616). Other drives and antivirus interaction not shown. |
| 5 | Memory: remember, correct, forget, export; secrets refused; guest mode | `memory/` (7 files) | Verified (fakes) | Real SQLite, real CLI probes (add, list, secret refusal, export, forget-all) on Linux. Survival across a real GUI close/reopen on a PC not shown. |
| 6 | Personalization: 17 personalities, five intensity controls, per-field recovery | `personality/` (9 files) | Verified (fakes) | Behaviour with a real model is unmeasured (`STAGE7_AUDIT.md`). |
| 7 | 18+ gating for Savage, Flirty, Sexy, Romantic; distress override | `personality/`, tests p55 | Verified (fakes) | Self-declaration gate that fails closed; distress detection is a keyword heuristic that will miss phrasings. Not an age-verification system. |
| 8 | Rename the assistant | `app/`, `personality/` | Implemented | 8 source files, 10 test files. |
| 9 | Voice: always-listening wake word, Vosk speech-to-text, SAPI and Kokoro speech, mute, hands-free turn-taking | `voice/` (14 files), `app/voice_service.py` | Verified (fakes) | Fake audio drives the real session logic. **No real microphone, speaker, Vosk model or Kokoro run exists.** A shutdown-ordering defect in the reply thread was found and fixed this session. |
| 10 | Speaker recognition (only respond to the owner's voice) | none | **Out of scope: owner decided it is not required (2026-10-29)** | No implementation or test. Not found in any of the 73 available prompts (only in older project notes). Kept open until the owner confirms it is required; wake-word detection alone does not identify who is speaking. |
| 11 | Permission-controlled computer operations | `desktop/` (23 files), `dev/`, `monitor/` | Verified (fakes) | One executor path, fingerprint-bound confirmation, audit, emergency stop; tested against a **simulated** desktop. Real windows, clicks, UI Automation, junction handling and OCR on Windows are not verified (R-5 in `PROMPT11_FINAL_AUDIT.md`). |
| 12 | Planning modes: missions, step approval, schedules | `missions/` (8 files), `planning/` | Verified (fakes) | Approval bound to plan hash, tamper detection, schedule limits, crash recovery tested with the real executor and fakes. No real tool side effects on Windows; a real model's plans unmeasured. |
| 13 | Offline by default; optional online only with consent, no silent cloud fallback | `net/policy.py`, `online/` | Verified (fakes); offline run Verified (real, older commit) | Every network feature off by default; one choke point; DNS-rebinding and NAT64 defects fixed earlier. Online research uses mocked HTTP; no real internet request. |
| 14 | Privacy controls and data deletion | `memory/`, `telemetry/`, `app/diagnostics.py` | Verified (fakes) | Telemetry endpoint is empty (inert). Diagnostics export contained no user name in a real CI run. Redaction is best-effort. |
| 15 | Windows packaging: PyInstaller exe, Inno Setup installer, silent install and uninstall keeping data | `packaging/`, `scripts/`, `.github/workflows/` | Verified (real) at the current commit for build and silent install | Built and installed on every push (run 40928361454). A clean consumer PC, upgrade-over-install and SmartScreen not tested. |
| 16 | Release security | `security/signing.py`, `updater/`, `scripts/release_tools.py` | Partial; signing **Blocked** | SHA-256 checksums are produced. Catalog and update manifests use Ed25519 verification, but no real keys or URLs exist. **The exe and installer are not code-signed** (needs a paid certificate): SmartScreen will warn. Not independently audited. |
| 17 | Game/FPS monitor, vision/OCR | `monitor/`, `desktop/ocr.py` | Implemented (fakes) | Parsers tested on fixtures; Tesseract absent on CI, no real PresentMon run. |
| 18 | Crash recovery, shutdown safety | `app/controller.py`, `inference/` | Verified (fakes) | Worker crash, hang and cancel tested with a fake runtime in real child processes. Intermittent Windows cleanup error `WinError 32` still **unexplained** (`VERIFICATION_LEDGER.md` section 5). |
| 19 | Accounts, admin panel, licensing backend | `docs/ACCOUNTS_ADMIN_DATABASE_PLAN.md`, `server/` | Deferred by the owner | Planned for after Windows validation; not part of this acceptance. |
| 20 | Original from-scratch model (own tokenizer/transformer) | `model/`, `train/`, `tokenizer/` | Research track only | Tiny, never trained; the shipped model is a third-party Qwen model, stated as such everywhere. |

## The 12 hands-free rules of Prompt 19 Part 9, mapped to evidence

Every row is **Verified (fakes)**: the real session logic driven by fake audio and fake speech engines. No row has run on a real microphone, speaker, Vosk model, SAPI or Kokoro.

| # | Rule | Evidence (code / test) |
|---|---|---|
| 1 | Microphone activation explicitly controlled | `VoiceService.enable/disable`, mute; tests for no start when disabled and idempotent start (`test_p17_handsfree`). **See open decision D: first start on a fresh profile enables wake-word listening.** |
| 2 | Microphone state visible | Home HUD chip, `hud_model`; `test_p70_hud`, `test_p71_hud_qt` (offscreen on Windows CI) |
| 3 | Wake word through the local stack | Vosk-based wake matching in `voice/session.py`; accuracy never measured |
| 4 | Request enters the same assistant logic as typed input | `_voice_respond -> reply_stream`; p17 tests |
| 5 | Spoken reply uses the selected voice and personality | p45/p46 (SAPI, Kokoro) with fakes |
| 6 | Listening resumes after a reply | p17 state-machine tests |
| 7 | Mic and speaker muted independently | 23 mute tests; `docs/VOICE.md` |
| 8 | Cancel / stop | `/stop`, `/normal`, shutdown tests; a reply-thread shutdown defect was fixed this session |
| 9 | Missing device or model gives a clear error | missing-model and unplug tests |
| 10 | Voice cannot approve protected actions | `test_spoken_yes_cannot_answer_a_pending_confirmation`, `test_spoken_permission_requests_change_no_permission_file_and_no_audit` |
| 11 | Audio not retained or uploaded | `voice_service.py` states it saves none; raw-audio test; no network code in `voice/` |
| 12 | Not active merely because installed | Partly: needs a sound device AND a Vosk model that Jarvis never installs itself; but see decision D |

Echo and self-wake: `_echo_of_own_speech`, guard window, `test_wake_phrase_inside_its_own_reply_does_not_self_trigger_or_interrupt`. Barge-in defaults to wake-word-only because no echo cancellation exists.

## Scorecard (Prompt 19 Part 17), re-scored at 60c8f61 (self-assessment, not independent)

Scale as in the prompt (8 = validated against meaningful acceptance criteria). **No category reaches 8.** Scores moved from `PROMPT19_REVIEW.md` (2026-10-26) only where CI evidence now exists. "Real" means hosted Windows CI at the older commit unless stated.

| # | Category | Score | Conf. | Evidence for the score | Biggest gap |
|---|---|---|---|---|---|
| 1 | Intelligence and reasoning | 4 | Medium | Real 48-case suite: Qwen3-1.7B 0.708, 4B 0.792 (heuristic, small sample, older commit) | 8B unmeasured; goal 0.85 not reached |
| 2 | Knowledge and factual accuracy | 3 | Low | Part of the same suite | no dedicated factual benchmark |
| 3 | Coding and debugging | 3 | Low | coding category 0.5-0.75; edit/rollback tools with fakes | Linux-only sandbox; no Windows code run |
| 4 | Research and web | 3 | Low | document reading with real files; HTTP mocked | no real search provider verified |
| 5 | Memory and personalization | 5 | Medium | retrieval measured; CLI probes; persistence tests | real-chat usefulness, GUI on a PC |
| 6 | Planning and task completion | 3 | Low | mission machinery with fakes | real-model missions |
| 7 | Windows computer control | 2 | Low | simulator only | any real desktop task |
| 8 | Hands-free voice | 2 | Low | 388 fake-audio tests | no hardware run at all |
| 9 | Personality and conversation | 3 | Low | style/safety separation tests | no real-model style evaluation |
| 10 | GUI and UX | 4 | Medium | real window clicked on CI (wizard, chat, cancel, damaged model) | no person has looked at it; DPI, a11y |
| 11 | Reliability and recovery | 4 | Medium | many failure-injection tests; flake open (3 of 18 Windows runs) | real OOM/crash/long-run tests |
| 12 | Security and privacy | 4 | Medium | negative tests, no-network guards, defects fixed | self-audit only |
| 13 | Offline operation | 4 | Medium | real offline reply with the app firewalled (older commit) | not on current code or a clean PC |
| 14 | Hardware compatibility and performance | 2 | High | one class measured: 2 vCPU, 11.15 tok/s, 2.4 GB | no GPU, no target PC, no matrix |
| 15 | Packaging and commercial readiness | 4 | Medium | installer built and silently installed on every push | unsigned, no clean PC, licence review pending |

Overall: about 3.5 out of 10. Not ready for everyday users and not ready for paying customers.

## Open decisions and conflicts (for the owner)

| ID | Item | Why it matters |
|---|---|---|
| A | Speaker recognition: required or not? | **Decided 2026-10-29: not required; ordinary wake word and voice controls stay** |
| B | Qwen3-8B: should the first-run wizard recommend it on capable machines? | It is the stated target but unmeasured; the wizard never selects it (`auto_recommend=False`). Per the owner's instruction it stays non-default until measured |
| C | Authorise one consented real-model run on the current commit (`REAL_INFERENCE_ONE_RUN.md`) | Only way to re-verify inference on the current code without a PC |
| D | Wake-word listening ON by default on a fresh profile vs Prompt 19 rule 12 ("never activate the microphone merely because it was installed") | The approved-UI prompt asked for always-listening "when enabled"; the code enables it at first start, but the microphone opens only if a sound device and a Vosk model exist, and Jarvis never installs the model. Decide: keep default-on with a first-run notice, or make it an explicit opt-in. Comments in the code that wrongly said "no-op unless enabled earlier" were corrected; behaviour is unchanged |
| E | Prompts 1 to 5 | Not available; send them if a requirement may live there |

## Release decision

**Not a release candidate.** Release-blocking items: (a) no real-model run on the current code; (b) no run on a person's Windows PC (GUI look and feel, DPI, microphone, speakers, desktop control); (c) no clean-machine install; (d) unsigned installer; (e) unexplained Windows test-cleanup flake, not yet shown fixed; (f) open decision 2 above; (g) Prompts 1-5 have not been provided and remain UNREVIEWED, so requirements in them are unknown. A preview build is fine for a person who will run `ACCEPTANCE_TEST_PLAN.md` and report back; it is not a product release.

## What would change this most, in order

1. One consented `windows-ci` real-model run on the current commit (about 1.28 GB download; needs the owner's go-ahead because it spends Actions minutes and bandwidth).
2. One pass of `ACCEPTANCE_TEST_PLAN.md` AT-05 to AT-14 on a real Windows 11 PC.
3. Owner answers to decisions 2 and 10.
4. A code-signing certificate, if the product will be sold.


## Owner decisions of 2026-10-29

* Always-listening (wake word) is ON by default on a fresh profile. Implemented by `AppController.voice_autostart`; the microphone opens only when a sound device and a local Vosk model both exist; an explicit OFF is remembered; mute and disable controls exist; no cloud speech fallback; no raw audio is retained (tests in `test_p17_handsfree.py`, `test_p39_no_silent_cloud.py`; all with fake audio, no physical microphone has been tested).
* Speaker recognition is not required.
* **Prompts 1-5 remain unreviewed** until the owner provides them. Their contents are not guessed anywhere in this repository.
