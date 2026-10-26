# Adaptive memory: how Jarvis adapts to you (and what it does NOT do)

## The one-paragraph version
Jarvis adapts to you by keeping **visible notes** about you in a local SQLite database and showing the relevant ones to the AI model
with each request. You can read, edit, export and delete every note. **Jarvis does not retrain or change the AI model from your
conversations**; nothing in this feature touches model weights. Everything stays on your PC unless you yourself export a file.

## Four different things that can change behaviour (kept apart, and logged separately)
| Kind | What it is | Who decides |
|---|---|---|
| Memory update | A row in your memory (e.g. "Prefers short answers"). The model is told about it. | Saved at once when you say so ("remember...", "from now on..."); suggestions by Jarvis wait for your approval |
| Profile adjustment | A personality setting (e.g. verbosity 0.2) | Only after you run `jarvis personality approve N` |
| Skill update | A new version of a reusable instruction you saved | You, or a pending suggestion you approve; old versions are kept as superseded |
| Model retraining | Changing the AI model's weights | **Never done from conversations.** The count in `jarvis memory learning` is always 0 and the code refuses to log it |

## Memory categories
`explicit_preference`, `approved_context`, `project`, `conversation_summary`, `procedure`, `correction`, `skill`, `temporary_task`.
Every row stores: category, text, **source** (user_said / user_confirmed / user_edit / assistant_inferred / assistant_summary /
verified_outcome), **confidence**, created/updated time, optional **expiry**, project, use count. Assistant-inferred items are always
*pending* and invisible to the model until you approve them. Temporary items expire after 24 hours and are deleted.

## What you can say (rule-based, so it is predictable; Jarvis tells you what it saved)
- "remember that I prefer metric units" -> saved; reply says the memory number and how to delete it.
- "remember for now that..." / "this time..." / "just for today..." -> temporary (24 h). Say "remember that" to make it permanent.
- "from now on keep answers short" -> standing preference, plus a *proposal* to change the verbosity setting (needs your approval).
- "keep answers short" (no durable words) -> applied for now only. Asked twice -> a standing preference is *proposed* (not active).
- "don't do that again" -> Jarvis asks what to avoid instead of guessing; "don't use bullet lists" -> saved as a correction.
- "no, my sister's name is Maria" -> a correction, kept 24 h unless you say "remember that".
- "use my usual style" -> uses your saved preferences, or says you have none.
- "do it like last time" -> uses a matching saved procedure/summary/project note, or says it has **no record** (the model is not asked, so it cannot invent one).
- "what did we discuss about X" / "resume my Garden project" / "what do you remember about me" -> answered from saved notes only; if there are none it says so.
- "forget about the cat" -> deletes the single matching memory, or lists candidates.
- Normal requests ("write a short poem") are not treated as preferences. The detector is deliberately narrow; it can still miss things or occasionally misfire, which is why every save is announced and reversible.

## Procedures and skills (verified outcomes only)
A procedure is saved **only after a run of it is verified** as successful by you ("that worked", or `jarvis memory procedure-verify N --worked`)
or by a tool check. A run nobody verified is stored as *unverified* and cannot become a procedure; failures are counted and lower confidence.
Today procedures come from `jarvis memory procedure-start NAME STEP...` (or code calling `offer_workflow`); the language model does not yet extract
them from chats by itself. Skills are named instructions you save with `jarvis memory skill-add`.

## Secrets are refused (best effort, not a guarantee)
Common shapes of passwords, API keys, tokens, private keys, payment-card numbers and ID numbers are refused; the reply says it was not saved without echoing the value.
This is pattern matching only (well-known key formats, "password is ...", Luhn-valid card numbers, long random-looking strings). **It does not catch every secret**
(for example an ordinary word used as a password, or a secret written in an unusual way, passes) and it can occasionally refuse harmless text. Do not put secrets in
memory notes, and check `jarvis memory list`. Summaries, crash logs, console error text and the personality-suggestion reason are passed through the same redaction,
which is equally best effort. The telemetry schema has no free-text fields and additionally refuses secret-looking values.

## Where it lives, and whose it is
- Default location: `%LOCALAPPDATA%\ProjectJarvis\memory.sqlite`. Windows gives every Windows user their own `%LOCALAPPDATA%`, so people with separate
  Windows accounts do not share it. `jarvis memory where` prints the exact paths.
- **Profiles** for several people on one Windows account: `jarvis --profile alice chat` (or set `JARVIS_PROFILE=alice`). Profile data lives in
  `...\ProjectJarvis\profiles\alice\` (memory, personality, voice settings, backups). The downloaded AI model is shared because it is not personal.
- **The file is not encrypted.** It is protected only by Windows file permissions; an administrator or anyone with your account can read it, and so can
  disk backups that include your profile folder. Profiles are separation for convenience, not a security boundary against someone who controls the account.
- **Guest session:** `jarvis --guest chat`. Nothing personal is read (the owner's memory is invisible) or saved; notes live in RAM and vanish when Jarvis closes;
  no summaries; personality changes are not saved; usage telemetry is skipped.
- Conversations are **not stored as transcripts**. Only notes (and optional summaries) are kept.

## Deleting: what is and is not guaranteed
- `jarvis memory delete N` removes the memory, **the older versions it replaced** (same category and key), and its search vector. It is then excluded from retrieval, the prompt, `list`, `search` and export.
- The database uses SQLite's default rollback journal (no WAL file), `secure_delete` is on, and `forget-all` runs `VACUUM`. In tests, deleted text was not found in the database file or any
  sibling file of the test folder on the Linux test machine. **That is evidence for that machine, not a guarantee.** SSDs (wear levelling), journaling or copy-on-write filesystems, OneDrive/File History/shadow copies,
  antivirus quarantine and disk backups can keep old copies that Jarvis cannot reach. Secure erasure cannot be guaranteed on SSDs or all filesystems.
- **Backups keep what existed when they were made.** `jarvis memory forget-all --yes --include-backups` also deletes this profile's automatic backups. Export files you saved yourself, copies you made, and Windows' own backups are not tracked.

## What a backup contains
`jarvis backup` copies **one profile's memory database** (memories, notes, workflow runs, learning log) into that profile's `backups` folder. It does **not** include personality or voice settings, network/telemetry consent settings, the online sign-in, or downloaded models. `restore` refuses corrupt files and backups from a newer version,
removes stale journal files, and keeps a safety copy of the current database. Backup/restore are unavailable in `--guest` mode. Upgrades by the installer make the same single backup of the default profile only and never delete data silently (the uninstaller asks, default No).

## Control commands
    jarvis memory list [--category C] [--all]   jarvis memory show N     jarvis memory search "words"
    jarvis memory add CATEGORY "text"           jarvis memory edit N "new text"      jarvis memory delete N
    jarvis memory pending | approve N | reject N          (suggestions waiting for you)
    jarvis memory export --out my-memory.json   (plain JSON of everything in this profile; keep it private)
    jarvis memory forget-all --yes [--include-backups]   (deletes memories, notes and logs in this profile; see "Deleting" for limits)
    jarvis memory where | learning | summaries [on|off]
    jarvis backup / jarvis restore FILE         (the current profile's memory database; the installer's automatic pre-upgrade backup covers the default profile only)

## Summaries and resuming projects
Automatic end-of-chat summaries are **off** by default (`jarvis memory summaries on`). When on, the local model writes a 3-sentence note when you close a chat of 3+
messages; secrets are removed; the note is a normal visible memory. Summaries are model-written and can be wrong (confidence 0.6, source `assistant_summary`).

## Retrieval
Default: local **lexical** matching (shared words, rarer words count more). It is not semantic. An optional embedding function can be plugged in for
semantic matching (`PersonalMemory(embedder=...)`); none is bundled and it has only been tested with a toy embedder.

## Training and sharing
Local memory is never sent anywhere and never used for training. The optional online service is separate and its learning is **OFF by default**, enabled only by typing `I consent` (docs/ONLINE.md). `--guest` blocks the online service.

## What reaches the model
Each turn the system prompt gets: the base prompt, memory rules ("say you have no record rather than inventing"), personality, then a saved-memory block with (a) your standing preferences, "Avoid:" corrections and unexpired "for now" notes, and (b) up to 5 other active notes that match the request. Pending suggestions, superseded, expired and deleted rows never appear. If you refer to an earlier conversation and nothing matches, the model is told explicitly that there is no record.

## What is tested, and what is not
Tested with real SQLite and a **fake, deterministic model** (it proves what reaches the prompt, not how well Qwen3 uses it; `tests/test_privacy_audit.py` labels fake vs real): all categories, expiry, secrets, pending approval, supersession, rule interpretation including false-positive guards, verified procedures,
skills, profiles, guest isolation (no files touched), export/delete/backup/restore, prompt contents, no-evidence answers bypassing the model, and a static scan that the memory
package has no network code. **Not tested:** real conversations with a real model, whether Qwen3 follows the saved-memory instructions well, usability of the phrase rules with real users
(English only), Windows file-permission behaviour, large memories (retrieval scans all rows; fine for thousands, untested beyond).
