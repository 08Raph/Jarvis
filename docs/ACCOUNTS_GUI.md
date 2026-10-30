# Accounts in the Jarvis window (sign up, sign in, manage)

Written 2026-10-29 on branch work/gui-auth. **Status: implemented. Logic tested against a real service over localhost; the page and the Windows token encryption were executed by real Qt (headless) and real DPAPI on the hosted Windows CI runner (run 40982262440, green; see VERIFICATION_LEDGER.md section 7). Nobody has used it on a real Windows PC. No hosted service exists.**

## 1. Three things that stay separate
1. **Local application access.** Jarvis never asks for an account to start, chat, remember, speak or use tools. There is no local login to lose.
2. **Online account.** Optional. Used only for the Jarvis Online service at an address *you* typed and confirmed. If the service is down, the Account page says so; nothing else changes, and an existing local session is never ended by a failed network call.
3. **Local AI.** The account code imports nothing from the assistant, inference engine, models or memory, and none of those import it (two tests check this). A test shows a local reply still works while a signed-in service is dead.

No artificial message limits, subscriptions, paid APIs or cloud inference were added. Local memory, files and microphone audio are never sent.

## 2. Before and after
| | Before (baseline 03ad89a) | After |
|---|---|---|
| Client | `jarvis online ...` command line only | Same CLI plus an **Account** page in the window (last item in the left list) |
| Identity | username + password, no email | unchanged (no email service exists, so nothing is called "email") plus an optional display name |
| Password hash | scrypt N=2^14, p=1 | scrypt N=2^16, r=8, p=2 (OWASP minimum); old hashes still verify and are upgraded at the next correct sign-in; stored parameters outside sane bounds are refused |
| Sessions | 30 days, fixed | 30 days, renewable (the old token dies at once), capped at 90 days from the original sign-in; sign out; sign out everywhere |
| Account changes | none | change display name; change password (needs the current password, signs out other sessions); delete account (password) |
| Lock-out reply | distinct "locked" message (revealed that the account exists) | identical to a wrong password or unknown user |
| Status | none | `status` column ('active' only today); a non-active account cannot use a session or sign in. **No admin tool exists** |
| Database | v1 | v2 (additive columns); a copy `<db>.v1.bak` is made before upgrading and never overwritten |

## 3. What the window does
Connect (explicit confirmation; https, or http to this same computer with a development box) -> Sign in or Create account -> Account (display name, change password, renew, sign out, sign out everywhere, delete account, disconnect). States shown: signing in / creating (buttons locked), invalid credentials, invalid username, weak password, password mismatch, existing username, terms or age not accepted, server unreachable, timeout, session expired (banner), success, logout confirmation, delete warning plus password.
* **Forgot password** explains plainly that there is no email: reset works only with the recovery code shown once at sign-up.
* **Remember me** is offered only where the token can be encrypted (Windows DPAPI, per Windows account). Elsewhere the box is disabled with the reason and the sign-in lives in memory only. On Windows the DPAPI code is exercised for real only by the Windows CI test `test_p77_dpapi_windows`.
* Passwords are never logged, written to configuration or memory notes, or kept after use; the boxes are cleared after each attempt.
* Guest sessions (`--guest`) disable the page.

## 4. Security measures (server)
Passwords: scrypt with a unique 16-byte salt; the recovery code is hashed the same way; session tokens and recovery codes are stored only as SHA-256 digests; every input is validated on the server (the window's checks are a convenience, and a test compares the two rule sets); SQL uses parameters only (injection strings tested); per-IP and per-account rate limits plus a 15-minute lock after 5 failures (in memory; they reset when the server restarts); equal-time handling of unknown users; no password hash or token digest in any response; audit events record the kind of event and the user id, never content or addresses.

## 5. Running a service to try it (no hosted service exists)
From the source folder, with Python 3.11+: `python -m jarvis_server --db data\jarvis.db` (set `PYTHONPATH=src;server`). It listens on 127.0.0.1:8080 over plain HTTP, which is **only acceptable on the same computer**. In the window: Account -> address `http://127.0.0.1:8080`, tick "runs on this same computer", Connect, then Create account. The installed Windows program does not include the server.

## 6. Limits and what is NOT done
* **Public use needs HTTPS** (a reverse proxy or `--tls-cert/--tls-key`) and a reviewed terms text; the server refuses a public address over HTTP or with `[OPERATOR: ...]` placeholders. No domain, certificate, hosting or deployment was created. TLS was never exercised here.
* **Email verification and email password reset do not exist** and are not faked; nothing says an email was sent.
* **No model sits behind the service**; online chat answers "no model connected". Sign-in works without one.
* **One local profile per Windows user folder.** Local memory is not partitioned by online account: signing out or switching online accounts does not change local data, and local data is not uploaded. Separate people should use separate Windows accounts (or `--data-dir`).
* Rate limits are in memory only; there is no CAPTCHA; the database is not encrypted at rest; the recovery code is the only recovery path.
* Terms and privacy text is a **draft template, not legal advice**.
* **Future (not built):** suspension and ban handling, an appeals flow, an admin panel and secure admin audit. Intended policy: an administrator sees account status; on an appeal they see the ban reason and only the relevant evidence, never unrestricted browsing of conversations; users can appeal; administrators can review and reverse. The `status` column and the audit-event table were prepared so this can be added later.

## 7. Windows 11 test steps (about 10 minutes)
1. Install Python 3.11+, unzip the source package, open a terminal in the folder. `set PYTHONPATH=src;server` then `python -m jarvis_server --db %TEMP%\jv.db`. Leave it running.
2. Start Jarvis. Open **Account** (last item). Expect the connect screen and the line that local Jarvis works without an account.
3. Address `http://127.0.0.1:8080`, tick the same-computer box, **Connect**, confirm. Expect the Sign in screen.
4. **Create account**: pick a username; enter two different passwords: expect the mismatch message; fix; leave "I accept" unticked: expect the terms message; tick both boxes; **Read the Terms**; **Create Account**. Expect a window with your recovery code: write it down. Expect the Account screen.
5. **Sign out** (confirm). **Sign In** with a wrong password: expect one generic message. Sign in correctly.
6. Change display name; change password; sign out; sign in with the new password.
7. Stop the server (Ctrl+C) and press **Renew session**: expect a plain "could not be reached" message, and Chat still answers locally.
8. Restart the server, **Delete account** (confirm, password): expect the sign-in screen and that the account no longer signs in.
Report anything that looks wrong with a screenshot. Remember me needs Windows DPAPI: tick it at sign-in, close and reopen Jarvis, open Account: you should still be signed in; Sign out should remove it.
