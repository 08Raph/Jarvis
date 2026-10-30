# Jarvis Online (optional sign-in service)

## Honest status
| Piece | State |
|---|---|
| Server (accounts, sign-in, sessions, consent records, rate limits, chat endpoint, export, delete, recovery) | Implemented in `server/jarvis_server`, standard library only. **Tested** on localhost over plain HTTP in this project's test suite |
| Windows client commands (`jarvis online ...`), sign-in required before any online chat | Implemented, **tested** against a real local server |
| **Account page in the window** (connect, create account, sign in/out, display name, change password, renew, delete) | Implemented; logic **tested** against a real local server; the page is tested with real Qt on a headless platform in CI only; **never used on a real PC**. See `ACCOUNTS_GUI.md` |
| Learning from conversations: **OFF by default**; ON only after you read the learning notice and type exactly `I consent`; one command to turn off (earlier messages withdrawn); `delete-chats` / `delete-account` | Implemented, **tested** (fake/localhost only) |
| Offline export of opted-in, scrubbed conversations for training | Implemented, **tested**. It does **not** train anything |
| **An AI model behind the service** | **Does not exist.** No model has been trained. With no model the service answers `503 no model connected` and saves nothing; the client says so. **Nothing in this project may be described as a working AI until a real trained model is connected and evaluated.** |
| TLS, real internet exposure, load, abuse resistance at scale | **Not tested.** TLS is delegated to a proxy/tunnel or `--tls-cert`; only loopback HTTP was exercised |
| Windows token encryption (DPAPI), the Windows build of the client | Written, **not run** |
| Terms / privacy text | A **draft template**, not legal advice. The server refuses to run publicly while `[OPERATOR: ...]` placeholders remain |
| No built-in server address | By design: the app contacts only a server URL the user typed and confirmed |

## What "free" can and cannot mean
The software and every dependency are free (Python standard library, no paid API, no subscription). **Running a service for the world is not free by itself**:
somebody's computer must stay on, have a public HTTPS address, and run the model. Realistic zero-cost starting points (check each provider's *current*
free-tier terms yourself; I could not verify them here):
- your own always-on PC or a spare machine / Raspberry Pi, exposed through a free tunnel or router port-forward + a free TLS certificate (Caddy gets one automatically if you have a domain; domains normally cost money);
- a cloud "always free" virtual machine if you qualify (often requires identity/card verification, and capacity or terms can change);
- start with friends on a private link, then decide about worldwide use.
CPU inference of a small model is cheap, but a public chat service attracts abuse; the built-in limits help but are not a substitute for monitoring.

## Privacy design (what is implemented)
- Sign-in required for every online chat; passwords hashed with scrypt; session tokens and recovery codes stored only as hashes; generic login errors; per-account lockout and per-IP/per-user rate limits.
- Consent: registration needs the current terms fingerprint and an age-16 confirmation. **Learning (training use) is a separate, second choice and is OFF unless you type exactly `I consent` after reading the learning notice** (`learning_notice_en.md`, served with its own fingerprint by `/v1/terms`). The server refuses to turn it on without that fingerprint (`learning_consent_required`). Changing the terms forces re-acceptance before chat.
- Two different things are stored, do not confuse them: (1) to answer you the service must receive your messages and keeps your history while your account exists (service function, not training; delete it any time with `jarvis online delete-chats` or `delete-account`); (2) a per-message `learn_ok` flag, which is 1 only if learning had explicit, current consent when the message was sent. Only flagged messages sent after the consent time can ever be exported for training.
- Turning learning off (`jarvis online learning off`) stops it and withdraws not-yet-used earlier messages (`--keep-past` to keep them). Text already used in a training batch cannot be removed from a model trained on it; the terms say so. If the operator edits the notice text, earlier consent no longer counts until you confirm the new text.
- **Existing accounts from the old ON-by-default design are not silently changed.** Their stored value stays as it was, but it is marked `legacy_default`, which is not consent: nothing of theirs is used for training until they run `learning on` and confirm. On sign-in the client tells them this. (No public deployment of this service exists, so this is a safety migration, not a known population.)
- Local personal memory (`jarvis memory ...`) is never sent to this service and never used for online training; a test scans the client for any memory import. `--guest` refuses all `online` commands.
- The audit log records event types and user ids, never message text, passwords or IP addresses. Server logs print method/path/status only.
- Account export and permanent deletion (password required) are built in.
- The client sends nothing until the user runs `online connect URL` and types `yes`, uses HTTPS only (plain HTTP to localhost only with the explicit development flag), and records every request in the in-memory audit.

## Things you must decide or do before public launch (not done here)
1. **Legal review** of the terms/privacy notice for every region you serve. Training use of chats is a high-risk area under laws such as GDPR (which generally requires a valid lawful basis, often explicit consent, plus rights handling) and for minors' data. The opt-in design (default off, explicit informed consent, an age gate, withdrawal, export, deletion) helps but is not a legal guarantee.
2. **No email**, so no email verification or email password reset; recovery depends on the recovery code the user saves. Fake or throwaway accounts are possible. CAPTCHA/phone checks are not included.
3. Moderation/abuse handling and a way for users to contact you.
4. Backups and their retention (the terms have a placeholder).
5. A trained model, evaluated honestly, before you tell anyone it answers questions.

## Run the server (free, no GitHub needed)
Python 3.11+; no installation of packages is needed for the server itself.

    cd project-jarvis\server
    set PYTHONPATH=%CD%;%CD%\..\src
    python -m jarvis_server --db C:\jarvis-data\service.db            (127.0.0.1:8080, no model: answers 503 "no model connected")
    python -m jarvis_server --db ... --model-dir C:\path\to\model     (needs PyTorch and a trained model; fails loudly if missing)

Public use requires HTTPS: `--tls-cert/--tls-key`, or put a TLS-terminating proxy in front and add `--behind-tls-proxy`
(also needed: your edited `--terms-file`). Example Caddy proxy (free, automatic certificates, needs a domain):

    jarvis.example.org {
        reverse_proxy 127.0.0.1:8080
    }

## Try it on one computer (development flag allows http to localhost only)
    python -m jarvis_server --db %TEMP%\j.db            (terminal 1)
Then, in a second terminal, use: `python -m jarvis online --dev-insecure-localhost connect http://127.0.0.1:8080`, then `terms`, `register`, `chat "hi"` (expect the honest "no model" message), `learning off`, `export`, `delete-account`.
(The flag goes right after `online` on every command.)

## Commands (client)
    jarvis online connect https://your-service     jarvis online terms      jarvis online register      jarvis online login
    jarvis online chat ["text"]   jarvis online learning [show|on|off] [--keep-past]   (on shows the notice and needs `I consent`)   jarvis online export [--out FILE]
    jarvis online delete-chats   jarvis online delete-account  jarvis online recover   jarvis online accept-terms   jarvis online logout   jarvis online disconnect
Local chat (`jarvis chat`) is unchanged, never touches the network, and does not require sign-in. Decide whether you want local mode to remain available.

## Using the learning data (offline, when you choose)
    python -c "from jarvis_server.db import DB; from jarvis_server.learning import export_training_data as e; print(e(DB('service.db'), 'train.jsonl'))"
The JSONL has no usernames and obvious personal data removed (an imperfect filter: review before training). Feed it through the project's provenance/dedupe/decontamination pipeline.


## The earlier conflict is resolved
The service used to default learning to ON (an earlier instruction), which conflicted with the later rule that nothing is used for training or transmitted for training without
explicit, informed opt-in. The later rule now governs: default OFF, explicit consent, `legacy_default` accounts paused. If you want the old behaviour back you would have to
change the code deliberately; it is not a setting.

## Limits that remain
- The notice and terms are drafts with `[OPERATOR: ...]` placeholders; they need legal review. Scrubbing of personal data in the export is regex-based and imperfect.
- Deleting chats or an account removes rows from the live database; the server's own backups, if you make any, and filesystem remnants are the operator's responsibility.
- Nothing here was tested over TLS, on Windows, or with a real model.
