# Accounts, admin panel and online database: preparation plan

**Status: PLANNING ONLY. Nothing in sections 2 to 10 is implemented.** Every table, policy, endpoint, role, job and deadline below is
**PLANNED, NOT IMPLEMENTED** unless a line says `[EXISTS]`. No Supabase project exists, no Supabase code, dependency, credential or
migration is in this repository, and nothing here has been tested. Written 2026-10-28 against commit `3cc23ff`.

Labels used: `[EXISTS]` verified in the repository at that commit; `[PLANNED]` proposed only; `[DECISION]` needs the owner (or a lawyer);
`[UNVERIFIED]` could not be confirmed from official documentation or the repository.

This plan must not change the order of work: finish the Windows integration and validation stages (see `docs/STAGE7_VALIDATION_RUNBOOK.md`
and the packaging milestones) first. Nothing here lifts the restriction on the adult personality styles (`ADULT_MODES_RESTRICTED`).

---

## 1. Current-state findings (read from the repository, not from earlier reports)

### 1.1 Local data `[EXISTS]`
| Store | File | What it holds | Controls that exist |
|---|---|---|---|
| Main memory DB (SQLite) | `src/jarvis/memory/store.py` | `conversations`, `messages`, `notes`, `action_log` (tool calls and output), `memories`, `memory_vectors`, `workflow_runs`, `learning_log`, `signals`; `PRAGMA user_version` schema check that refuses a newer database | `export_all()`, `delete_conversation()`, `delete_note()`, `delete_all()` |
| Personal memory | `src/jarvis/memory/personal.py` | structured personal facts | `PRAGMA secure_delete=ON`, `purge_expired()`, `forget_everything()`, `export()` |
| Data provenance | `src/jarvis/data/provenance.py` | source records for datasets | `export_manifest()` |
| Backup helpers | `src/jarvis/storage.py` | online backup, `PRAGMA integrity_check`, schema-version check on restore | backup and restore verification |

All of it is on the user's PC. None of it is sent anywhere by default.

### 1.2 An online service already exists `[EXISTS]` (and it matters for this plan)
`server/jarvis_server` is a standard-library-only HTTP service with its own SQLite file. It is deliberately outside `src/jarvis` so it is never bundled
into the Windows client. Tables (`server/jarvis_server/db.py`): `users`, `sessions`, `messages`, `telemetry`, `events`.
Routes: `/v1/register`, `/v1/login`, `/v1/logout`, `/v1/me`, `/v1/accept-terms`, `/v1/settings/learning`, `/v1/chat`, `/v1/messages/delete`,
`/v1/account/export`, `/v1/account/delete`, `/v1/recover`, `/v1/telemetry`, `/v1/telemetry/delete`, `/v1/terms`, `/v1/health`.

What it does: scrypt password hashes, hashed session tokens, hashed recovery code, lockout and rate limits, terms fingerprint, an "I am at least 16"
boolean (`age_confirmed`), a separate opt-in for using conversations as training data, export, permanent deletion, and a minimal `events` log
(type, time, user id; no message text).

What it does **not** have: roles, an admin interface, email (so no email verification or reset), bans or sanctions, appeals, evidence handling, an
admin audit trail, a date of birth or age band, entitlements for restricted features, retention jobs for messages, or backups. There is no AI model behind it
(`503 no model connected`). According to `docs/ONLINE.md` it was exercised only on loopback HTTP; TLS, real exposure, load and Windows operation are untested
(Verified on 2026-10-28: `tests.test_online`, 33 tests, passes in the authoring sandbox; that is a localhost unit-test result, not a deployment test. **Deployment state is unknown:** no server database exists in this repository or its history, but a real one could exist on whatever machine ran the server with `--db`; see `docs/ONLINE.md` and the read-only check in section 1.5.)

**Findings that conflict with the new requirements** (to resolve before building anything):
1. **Verified in code (`server/jarvis_server/app.py`, `chat`):** every `/v1/chat` request carries up to 40 messages (8,000 characters each, 32,000 in total), but only the **last user message and the model reply** are written to `messages.content`, and only when a model replied (with no model the route answers `503` and saves nothing). Those rows stay for as long as the account exists. The new requirement is *no unnecessary private conversation content in the online database*; the server also handles the full request text in memory on every call. See section 3. Whether any such rows exist in a real deployment is **unknown**.
2. `age_confirmed` is a self-declaration of 16+. The new requirement is age *groups* and verification status, enforced by trusted backend logic.
3. The telemetry table (`install_id`, pseudonymous, 180-day retention) is intentionally unlinked from accounts and must stay that way.
4. **Verified in code:** `terms_en.md` says backups, "if any", are encrypted at rest, but no server code creates or encrypts backups, and the database is a plain SQLite file with no secure-delete setting. The `events` table keeps `user_id` on rows such as `messages_deleted` after the account is deleted (only `account_deleted` itself is logged with a null id). Both are risks for the deletion and retention design in section 5.4.

### 1.3 Other relevant facts `[EXISTS]`
- `src/jarvis/online/` is the client (`client.py`, `flows.py`, `tokenstore.py`). It talks only to a server URL the user typed and confirmed; local chat never touches the network. Token encryption with Windows DPAPI is written but has never run on Windows.
- `src/jarvis/security/signing.py` already provides Ed25519 signatures over canonical JSON (used for update manifests). It is a candidate for signed entitlement tokens (section 4.6).
- Adult personality styles carry `adult_only=True` and are blocked locally by the constant `ADULT_MODES_RESTRICTED = True` in `src/jarvis/personality/styles.py`. There is **no** age or date-of-birth logic anywhere in the client.
- A grep of the repository for "supabase" returned no matches in code, configuration, tests or docs before this document was added.
- The Supabase MCP tools available in the authoring session were **not used**. All Supabase facts below come from official documentation pages read on 2026-10-28.

### 1.4 What remains to be built (all `[PLANNED]`)
Everything in sections 2 to 9: a hosted Postgres project, account sign-up through a managed auth provider, role-based staff access, the moderation and appeal
data model, evidence handling, an audit log, retention and deletion jobs, backups, and a separate admin panel.

### 1.5 Checking a real deployment, read-only `[PLANNED procedure; not run]`
No database file was found in this environment, which says nothing about the operator's machine. On the machine that ran `python -m jarvis_server --db <file>`, stop the server or work on a copy, then run counts only (never `SELECT *`):
```
sqlite3 -readonly <db file> "select count(*) from users; select count(*) from sessions where expires_at > cast(strftime('%s','now') as integer); select count(*) from messages; select kind, count(*) from events group by kind;"
```
Do not open the `users`, `sessions` or `messages` columns, and do not paste the file anywhere.
Without the `sqlite3` command line tool (typical on Windows), the same counts with Python, opened read-only:
```
python -c "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); print([c.execute(q).fetchall() for q in ('select count(*) from users','select count(*) from messages','select kind,count(*) from events group by kind')])" "<db file>"
```
Counts above zero mean real data exists and sections 4 to 5.4 of this plan apply to it; a zero count only describes that one file.

---

## 2. Proposed architecture `[PLANNED]`

### 2.1 Why Supabase, and what it does not solve
Supabase provides managed Postgres, an auth service with bcrypt-hashed passwords, Row Level Security, and Edge Functions. A free plan exists (section 7).
It does not solve local inference (which stays local), legal review, email deliverability, or abuse handling.

Alternatives considered:
| Option | For | Against |
|---|---|---|
| Keep and extend the stdlib server `[EXISTS]` | already written, zero hosting vendor, SQLite | no email, no RLS, you operate TLS, backups and uptime; single SQLite file is a weak fit for admin roles and audit integrity at any scale; untested publicly |
| **Supabase (recommended as the initial backend)** | managed auth, Postgres constraints, RLS, triggers, Edge Functions, free tier | free-tier pausing and no automatic backups; vendor lock-in is moderate (it is standard Postgres); policy mistakes are the main risk |
| Self-hosted Postgres plus a custom service | full control | you run everything; most work |

Recommendation: use Supabase for accounts and admin data only, keep `server/jarvis_server` as a reference and as the zero-cost fallback, and **do not migrate its code**:
reuse its ideas (consent fingerprints, recovery, export, delete) in the new design. There may be no real user data in the old service; I cannot know (`[DECISION]`
confirm whether any real accounts exist before deleting or migrating it).

### 2.2 Components
```
Windows client (local inference, local SQLite)         Admin panel (separate project, separate origin)
   |  publishable key + user JWT only                      |  staff JWT (MFA required) only
   v                                                       v
 Supabase Auth  --------------------------------------  Edge Functions (admin actions, entitlement tokens)
   |                                                       |  secret key lives ONLY here (function secrets)
   v                                                       v
 Postgres: public (RLS, user-owned rows)  |  admin (no direct client access)  |  private (backend only)
```
Rules:
- The Windows client and the admin panel front end hold **only** the publishable key. The secret key (`service_role`) bypasses every RLS policy and is allowed only in backend components (Edge Function secrets). It never ships in the installer, the repository or a browser.
- Every staff action is an Edge Function (or a `SECURITY DEFINER` function) that re-checks the caller's role and MFA level server-side, performs the change and writes the audit row **in the same transaction**.
- Online account services stay separate from inference: the client never needs a network connection to generate text, and no code path sends prompts to a cloud model.

### 2.3 Environment and secrets `[PLANNED]`
- Separate Supabase projects for `dev`, `staging`, `prod`. Free plan allows two active projects, so staging may have to be local (Supabase CLI) instead.
- Configuration via environment variables or the host's secret store, never committed. `.gitignore` entries and a secret-scanning check (like the upload guard in `scripts/ci_stage_upload.ps1`) are part of acceptance tests.
- One secret key per backend component so a leak has limited reach; a rotation runbook (create new key, replace everywhere, confirm, retire) is written before launch.
- Client builds receive the project URL and publishable key at build time. Per `docs/TELEMETRY.md`, builds without a server address contain none; this stays true for account features (`[DECISION]` whether an account-enabled build is a separate release channel).
- Jarvis's local memory must never hold credentials or tokens. Session tokens go in the token store (DPAPI), not in `memories`; a test scans for this (section 9).

---

## 3. Data ownership: local versus online `[PLANNED]`

| Data | Where | Reason |
|---|---|---|
| Chats, prompts, replies, notes, memories, memory vectors, voice data, tool action log, workflow runs, personality preferences, model files, settings | **Local SQLite / files only** | local-first promise; unchanged |
| Account identity (id, handle or email, status), terms and consent records, age band and verification status, feature entitlements | **Online** | needed to authorise and moderate |
| Sanctions, appeals, admin-written case notes, evidence items, evidence access log, admin audit log | **Online** (admin-only access paths) | accountability |
| Deletion requests and minimal tombstones | **Online** | lifecycle and re-application after restores |
| Telemetry | **Online, separate and unlinked** (existing design) | opt-in, pseudonymous |
| Date of birth, if collected at all | **Online, private schema only**, never exposed to admins | `[DECISION]` see 5.3 |
| Online chat history on the server | **Not stored by default** `[DECISION]` | the old service stores it; recommended to remove (below) |

**Evidence and the local-first conflict.** Admins must be able to see *relevant* conversation evidence for an appeal, but conversations are local. Options:
| Option | Evidence comes from | Effect |
|---|---|---|
| A (recommended) | Content that actually passed through an **online** service (if any), kept as a short-lived snapshot attached to a moderation case when a rule fires, plus text the **user chooses to attach** to an appeal | no standing copy of everyone's chats; the user knows what is retained |
| B | Client auto-uploads flagged local excerpts | breaks the local-first promise; not recommended |
| C | Keep full server-side history | contradicts the requirement; not recommended |
If local chat never touches the server, a ban can only rest on online conduct or user-supplied material. `[DECISION]` what conduct the service can ban for, since local-only use is not observable.

**Honest limit on enforcement.** Inference and the personality system run on the user's own machine. A server can decide whether to issue an entitlement
and can refuse online services, but it cannot stop a modified local client from ignoring a flag. Local gating (`ADULT_MODES_RESTRICTED`, UI hiding) plus a
signed entitlement is policy and friction, not a security boundary against the PC's owner. Server-side enforcement is real only for server-mediated
features. Do not describe it otherwise.

---

## 4. Authentication, authorization, RLS and threat model

### 4.1 Account lifecycle `[PLANNED]`
1. **Register:** email and password through Supabase Auth, terms version hash accepted, age declaration (5.3). Email confirmation required. The built-in email provider is limited (2 emails per hour per the auth rate-limits page), so launch needs a custom SMTP provider `[DECISION]` (may need a domain; cost unverified).
2. **Sign in / out / manage:** managed by Supabase Auth; the client stores the session in the DPAPI token store; sign-out revokes the refresh token.
3. **Recovery:** email-based reset. The old recovery-code scheme `[EXISTS]` is not carried over unless email is rejected `[DECISION]`.
4. **Status values:** `active`, `suspended`, `banned`, `deletion_pending`, `deleted`. Status lives in `public.profiles` and is changed only by backend functions.
5. **Passwords:** Supabase Auth stores bcrypt hashes (documented). Never plaintext. Leaked-password protection is documented as **Pro plan only**; on the free plan enforce length and character rules and add a breached-password check in an Edge Function if wanted `[DECISION]`.

### 4.2 Roles `[PLANNED]`
Roles are rows in `admin.staff_roles` (never `user_metadata`, which the user can edit; the RLS docs recommend `app_metadata` or tables). Proposed roles:
| Role | May do |
|---|---|
| `support` | view the account directory (no DOB, no evidence), see statuses, add non-evidence notes |
| `moderator` | everything `support` can, plus open cases, impose warnings and suspensions, open **assigned** appeal evidence |
| `admin` | decide appeals, impose and lift bans, grant or revoke entitlements, manage staff below owner |
| `owner` | manage admins; cannot be removed by others; break-glass access is logged and alerts the others |
Rules: staff must use MFA (TOTP, AAL2) `[PLANNED]`; the person who imposed a sanction cannot decide its appeal; role grants and revocations are audited; claims added to the JWT by a custom access-token hook are stale until refresh, so staff access tokens are short-lived and every sensitive function re-reads the role from the table.

### 4.3 Tables (PLANNED; sizes and exact types to be settled in migrations later)
Conventions: `uuid` primary keys (`gen_random_uuid()`), `timestamptz` times, enums or `CHECK` constraints, foreign keys with explicit `ON DELETE` rules, an index on every column used in a policy filter.

| Table (schema) | Key columns | PK / FKs | Constraints and indexes |
|---|---|---|---|
| `profiles` (public) | `id`, `handle`, `status`, `terms_version_sha`, `terms_accepted_at`, `age_band`, `age_verification_status`, `created_at`, `deleted_at` | PK `id` = `auth.users.id` (FK, `ON DELETE RESTRICT`) | `status` CHECK in set; unique lower(`handle`); index `status` |
| `consents` (public) | `id`, `user_id`, `kind`, `doc_sha`, `accepted_at`, `withdrawn_at` | FK `user_id` | unique (`user_id`,`kind`,`doc_sha`); index `user_id` |
| `private.age_records` (private) | `user_id`, `dob` or `adult_declared_at`, `method`, `verified_at`, `band_recheck_on` | PK/FK `user_id` | no grants to any API role; read only by backend functions |
| `entitlements` (public) | `id`, `user_id`, `feature`, `state` (`never`,`granted`,`revoked`,`suspended`), `granted_by`, `reason_code`, `granted_at`, `expires_at` | FK `user_id`, `granted_by` | unique (`user_id`,`feature`); restricted features cannot be `granted` unless `age_band` and verification pass (trigger) |
| `admin.staff_roles` | `user_id`, `role`, `granted_by`, `granted_at`, `revoked_at` | PK (`user_id`,`role`,`granted_at`) | at most one active role per user; index (`user_id`) WHERE `revoked_at IS NULL` |
| `admin.cases` | `id`, `user_id`, `opened_by`, `opened_at`, `reason_code`, `summary`, `status` | FK `user_id`, `opened_by` | `status` CHECK; index (`user_id`,`status`) |
| `admin.sanctions` | `id`, `case_id`, `user_id`, `kind` (`warning`,`suspension`,`ban`), `imposed_by`, `starts_at`, `ends_at`, `notice_sent_at`, `appeal_deadline`, `lifted_at`, `lifted_by`, `lift_reason` | FK `case_id`, `user_id` | CHECK `ends_at > starts_at`; ban has null `ends_at`; index (`user_id`,`lifted_at`) |
| `admin.appeals` | `id`, `sanction_id`, `user_id`, `submitted_at`, `statement`, `status`, `assigned_to`, `due_by`, `decided_at`, `decision`, `decision_reason`, `legal_hold` | FK `sanction_id`, `user_id`, `assigned_to` | one open appeal per sanction (partial unique index); `assigned_to` ≠ sanction `imposed_by` (trigger) |
| `admin.evidence_items` | `id`, `case_id`, `appeal_id`, `source`, `created_at`, `content_ref`, `content_sha256`, `relevance_note`, `retention_class`, `delete_after`, `hold` | FK `case_id`, `appeal_id` | `delete_after` required unless `hold`; a retention job never deletes a row whose appeal is open or on hold |
| `admin.evidence_access_log` | `id`, `evidence_id`, `staff_id`, `appeal_id`, `purpose`, `at` | FKs | append-only; written by the access function itself |
| `admin.audit_log` | `id` (bigint identity), `at`, `actor_id`, `actor_role`, `action`, `target_user_id`, `case_id`, `request_id`, `detail` (jsonb, size-limited, no message text), `prev_hash`, `row_hash` | none on `actor_id` (must outlive the account) | insert only via function; `UPDATE`, `DELETE`, `TRUNCATE` revoked from every API role; hash chain makes edits detectable |
| `deletion_requests` (public/admin) | `id`, `user_id`, `requested_at`, `scheduled_for`, `status`, `blocked_reason`, `completed_at` | FK `user_id` (nullable after completion) | one active request per user |
| `deletion_tombstones` (private) | `id`, `user_hash`, `reason_class`, `created_at`, `expires_at` | none | minimal; used to re-apply deletions after a restore and, if lawful, to keep a ban from evaporating `[DECISION]` |

Telemetry stays in its own tables or project and is never joined to `profiles`.

### 4.4 RLS policy intent `[PLANNED]`
Enable RLS on every table in an exposed schema (documented best practice; with RLS on and no policy, the publishable key sees nothing). The `admin` and `private` schemas are **not exposed** through the data API at all; staff reach them only through functions.
| Table | `anon` | signed-in user | `support` | `moderator` | `admin` |
|---|---|---|---|---|---|
| `profiles` | none | read/update own non-status fields | read via directory view only | same | same, plus status changes by function |
| `consents` | none | read/insert own | none | none | read |
| `entitlements` | none | read own | read | read | read; write by function |
| `cases`, `sanctions` | none | read **own sanctions** (reason shown to the user, no admin notes) | read | read; create via function | write via function |
| `appeals` | none | insert/read own; no edit after submit | read status | read assigned | decide via function |
| `evidence_items` | none | none | **none** | **only via** `open_appeal_evidence(appeal_id, purpose)` for assigned, open appeals | same rule |
| `audit_log` | none | none | none | none | read; nobody writes directly |
| `private.*` | none | none | none | none | none (backend only) |
Policy rules: use `(select auth.uid())` wrappers and indexes for speed; explicit `auth.uid() IS NOT NULL`; no policy depends on `user_metadata`; `SECURITY DEFINER` functions set `search_path = ''`; execute rights on hook and admin functions are revoked from `anon`, `authenticated` and `public`.

### 4.5 Admin data visibility
- **Account directory view:** id, handle (or masked email), `status`, `age_band`, `age_verification_status`, created date, active sanction summary. **No DOB, no exact age, no password data, no conversation content.**
- **Appeals:** the ban reason and the evidence items attached to that appeal, nothing else. Opening evidence needs an open appeal, an assigned reviewer, a stated purpose, and writes an access-log row. There is no "search all conversations" function, and none will be created.

### 4.6 Age-restricted features `[PLANNED]`
A feature token (Ed25519-signed with the existing `jarvis.security.signing`, short expiry, bound to account id) is issued by a backend function only if all hold: status `active`; `age_band` allows the feature; verification status is `verified`; entitlement `granted`; no active sanction that blocks it. The client treats a missing, expired or unverifiable token as "not allowed" (fail closed) when offline. See the enforcement limit in section 3. The global code restriction on the adult styles remains until the separately reviewed change that lifts it.

### 4.7 Threat model `[PLANNED]` (none of these mitigations is tested)
| Threat | Mitigation |
|---|---|
| Secret key leaked (repo, installer, logs) | backend-only, per-component keys, secret scanning in CI, rotation runbook; publishable key assumed public |
| Missing or wrong RLS on a table | RLS on by default in migrations; automated test that fails when any exposed table lacks RLS; policy test matrix (section 9) |
| Malicious or curious admin | least privilege, no bulk conversation access, evidence only via logged function, audit hash chain, a second admin sees staff actions |
| Compromised admin account | MFA required, short staff tokens, alerts on role changes, break-glass logging |
| User edits their own role or age in metadata | authorization data only in tables / `app_metadata`; test attempts it |
| Stale JWT claims after ban or demotion | re-check status and role in tables in every sensitive function; short token lifetime |
| Ban evasion by re-registering | rate limits, email verification, optional tombstone hash `[DECISION, legal]` |
| Account enumeration / credential stuffing | generic errors, Supabase rate limits (documented defaults), lockouts, CAPTCHA `[DECISION]` |
| Modified local client ignores entitlements | accepted limit; server controls only server-mediated features |
| Leakage via logs and backups | no message text, passwords or tokens in logs or `detail`; backups encrypted; log retention is only 1 day on the free plan so the audit log is our own table |
| Free project paused or deleted | health ping, export schedule, upgrade trigger (section 7) |
| Supabase outage | local features keep working; account features degrade, entitlements fail closed |
| SQL injection through functions | parameterised queries, no dynamic SQL, `search_path = ''` |

---

## 5. Privacy, age, bans, appeals and deletion

### 5.1 Principles
No plaintext passwords; no secrets in Jarvis memory; no unnecessary conversation content online; admins see only what a case needs; every admin action is audited; deadlines are explicit and the system never silently deletes evidence needed for a pending appeal.

### 5.2 Ban, appeal and deletion lifecycle (proposed; **all periods are `[DECISION]` placeholders that need legal review**)
```
case opened -> sanction imposed (notice_sent_at, appeal_deadline = notice + 30 days)
   -> [no appeal] sanction stands; evidence delete_after = appeal_deadline + 30 days
   -> [appeal submitted <= appeal_deadline] evidence frozen (hold) ; reviewer assigned (not the imposer) ;
        acknowledge within 7 days ; decision due within 30 days of submission
         -> upheld    : evidence delete_after = decision + 90 days (or legal hold)
         -> overturned: sanction lifted_at set; evidence deleted after the hold; entitlements NOT restored automatically
   -> account deletion request during a sanction or open appeal: deferred until resolved
```
| Event | Timestamp / deadline recorded | Audit entry |
|---|---|---|
| Case opened, sanction imposed | `opened_at`, `starts_at`, `notice_sent_at`, `appeal_deadline` | actor, reason code, target |
| Appeal submitted | `submitted_at`, `due_by`, hold set | actor = user |
| Evidence opened | access-log row with purpose | staff id, appeal id |
| Decision | `decided_at`, `decision`, `decision_reason` | reviewer |
| Lift / unban | `lifted_at`, `lifted_by`, `lift_reason` | actor |
| Deletion requested / completed | `requested_at`, `scheduled_for`, `completed_at` | actor |
Exception handling: late appeal accepted only by an `admin` with a recorded reason; missed internal deadlines are flagged on the queue, never auto-resolve the appeal; reviewer conflict of interest reassigns the appeal; legal hold overrides every delete timer; an outage pauses deadlines and the pause is recorded; unreachable users (bounced email) still get the notice recorded in-app; minors and special cases follow the age policy `[DECISION]`.

**Unban rule.** Lifting a sanction sets `status = active` and **does not** change entitlements. Restricted features stay `revoked` or `never` until the age and verification checks pass again and an `admin` grants them. A test asserts this (section 9).

### 5.3 Age information `[DECISION]`
The existing service uses a 16+ self-declaration. Options:
| Option | Notes |
|---|---|
| Declaration only (`adult_declared_at`), no DOB | minimum data; weakest assurance |
| DOB in `private.age_records`, derive `age_band`, expose only band and `age_verification_status` | satisfies "admins never see DOB"; DOB is still personal data |
| Third-party age verification | stronger, may cost money and adds a processor; out of scope until decided |
Recommended default: collect the least that the legal review allows, keep any DOB only in the private schema, expose `age_band` (for example `unknown`, `16_17`, `18_plus` - the bands themselves are a `[DECISION]`) and `verified` / `unverified` to the admin view, and re-evaluate the band by a backend job so nobody needs DOB in the UI. Avoid storing the date a minor turns 18 outside the private schema, since it reveals the DOB.

### 5.4 Deletion and retention (starting proposal)
| Data | Retention | Notes |
|---|---|---|
| `profiles`, `consents`, auth record | until deletion completes, grace period 14 days (cancellable) | `[DECISION]` period |
| Entitlements | deleted with the account | |
| Sanctions, cases | kept for a limited period after deletion only if a legal basis exists | minimised; reason text reviewed |
| Evidence | per lifecycle above; deleted when no appeal is open and no hold | pending appeals never lose evidence |
| Audit log | long (for example 2 years) with user identifiers reduced to an opaque id after deletion | `[DECISION]` |
| Legacy `events` rows (old server) | keep user ids today after account deletion `[EXISTS]`; the new design should de-identify them | |
| Deletion tombstone | short, hashed | exists so a restored backup does not resurrect deleted people |
| Telemetry | 180 days as today; separate delete by install id | unlinked |
| Backups | deleted data remains until the backup ages out; restore procedure re-applies tombstones | state the lag in the privacy notice |
| Caches | session tokens cleared at sign-out; admin panel must not cache evidence; client caches wiped on account removal | |
Export on request returns the user's own profile, consents, sanctions and appeal records (excluding other people's data and security-sensitive internal notes `[DECISION]`).

---

## 6. Backup and recovery `[PLANNED]`
- **Free plan has no automatic backups** per the pricing page ("Not included"). The backups page recommends that free projects export regularly with the CLI and keep off-site copies; the production checklist says backups are not downloadable on Free. These statements overlap but are not identical (`[UNVERIFIED]` exact free-plan backup access).
- Plan: scheduled encrypted logical dumps (`supabase db dump` or `pg_dump`) stored off-site, at least weekly during testing and daily before real users; **restore drills** to a fresh project with RLS and role checks re-run after every drill.
- Daily backups (7-day retention on Pro) and PITR (a paid add-on, requires at least Small compute) are the paid route; the trigger to buy them is "real users with appeals pending" `[DECISION]`.
- **Storage API objects are not included in database backups** (documented). Do not store evidence there unless a separate export exists; prefer database rows.
- Restore procedure: create a project, apply migrations, restore data, re-apply `deletion_tombstones`, re-check RLS and role tables, rotate keys, announce any gap. Recovery targets (how much loss is acceptable) are a `[DECISION]`.
- If a free project is paused, data is kept; the docs give a one-year window to restore from the dashboard in prose, while a screenshot on the same page shows "90 Days Remaining" (`[UNVERIFIED]` which is current). After the window the backup and Storage objects can be downloaded.

---

## 7. Verified free-tier limits and possible costs (official pages read 2026-10-28)
Sources: [pricing](https://supabase.com/pricing), [billing guide](https://supabase.com/docs/guides/platform/billing-on-supabase), [pausing FAQ](https://supabase.com/kb/faqs/platform/free-project-pausing), [backups](https://supabase.com/docs/guides/platform/backups), [upgrading / paused projects](https://supabase.com/docs/guides/platform/upgrading), [API keys](https://supabase.com/docs/guides/getting-started/api-keys), [RLS](https://supabase.com/docs/guides/database/postgres/row-level-security), [RBAC](https://supabase.com/docs/guides/api/custom-claims-and-role-based-access-control-rbac), [password security](https://supabase.com/docs/guides/auth/password-security), [auth rate limits](https://supabase.com/docs/guides/auth/rate-limits), [production checklist](https://supabase.com/docs/guides/deployment/going-into-prod).
| Item | Free plan | Pro (for comparison) |
|---|---|---|
| Price | $0 | from $25 per month |
| Monthly active users | 50,000 | 100,000 then metered |
| Database size | 500 MB per project | 8 GB then $0.125 per GB |
| File storage | 1 GB | 100 GB |
| Egress | 5 GB | 250 GB |
| Edge Function invocations | 500,000 | 2 million |
| Active projects | 2 (paused projects do not count) | one included, others from $10 per month |
| Inactivity | **paused after about a week with too little database activity**; data kept | not paused |
| Automatic backups | not included | daily, 7-day retention |
| Point-in-time recovery | not included | add-on, from $100 per month for 7 days |
| API/database log retention | 1 day | 7 days |
| Leaked-password protection | not available | available |
| Built-in auth email | 2 emails per hour (project-wide); a custom SMTP server changes the limit | same page applies; not separately verified for Pro |
**Not verified / open:** what exactly happens on the free plan when a quota is exceeded (the pages I read do not say); whether the free plan will stay free (no page promises it; check before launch and again at launch); the legacy `anon` and `service_role` keys are documented as deprecated "by the end of 2026" so a new project should use publishable and secret keys; production auth email limits are described inconsistently across pages (2 per hour versus 30 new users per hour with custom SMTP); no spend-cap statement was on the pages I fetched except that Pro has one by default.
**Likely costs:** custom SMTP and a domain (unverified), the Pro plan once backups, no-pause and longer logs are needed, and possibly age verification. Nothing in this plan requires paying today; launching to real users on the free plan accepts the pause and backup risks above.
**Size caution:** 500 MB is small for audit logs and evidence snapshots; measure row sizes in staging before assuming it fits. `[UNVERIFIED]`

---

## 8. Implementation sequence and dependencies `[PLANNED]`
Prerequisites (not started): the Windows packaging and clean-machine, offline and recovery validation milestones; the real-model personality review; legal review of terms, privacy notice and retention; the decisions in section 10.
1. **Decisions and legal review** (section 10).
2. **Local-only schema work:** migration files and RLS test suite run against a local Supabase (CLI plus Docker) or a throwaway Postgres. No hosted project yet.
3. **Auth skeleton:** register, sign in, sign out, confirmation email through a test SMTP; token storage; failure UI.
4. **Consent and age model:** consent records, age band logic, entitlement table, feature-token issuing, fail-closed client behaviour.
5. **Admin panel, read only:** staff login with MFA, account directory view, audit viewer.
6. **Moderation:** cases, sanctions, notices, appeals, evidence functions, access log, deadlines and queue.
7. **Deletion, export and retention jobs;** tombstones.
8. **Backup and restore drills;** key rotation drill.
9. **Security review and an external test of RLS and admin flows;** limited beta.
Dependencies: 3 needs custom SMTP; 4 needs the age decision; 6 needs the evidence-source decision; 7 needs legal retention periods; 8 needs a decision on the paid plan.

---

## 9. Tests and acceptance criteria required before launch `[PLANNED]` (none run)
- **RLS matrix:** for every table and operation, run as `anon`, a normal user, another user, `support`, `moderator`, `admin`, `owner`; assert allowed and denied cases, including cross-user reads, writes to own `status`, own `age_band`, own `role` metadata, and direct reads of `admin.*` / `private.*`. A guard test fails when any exposed table lacks RLS.
- **Impersonation tests** with set JWT claims in a local database; **stale-claim tests** (demoted or banned user still holding a token).
- **Admin visibility:** the directory view never returns DOB, exact age or content; evidence is unreachable without an open, assigned appeal; each access writes a log row; no function exists that searches conversations.
- **Audit:** every admin action produces one audit row in the same transaction; update, delete and truncate are impossible; the hash chain verifies and detects tampering.
- **Lifecycle:** deadlines computed correctly; pending appeal blocks evidence deletion and account deletion; late-appeal and conflict-of-interest paths; unban leaves restricted entitlements untouched; entitlement tokens refused for banned, unverified or expired states.
- **Deletion and backups:** deletion removes or de-identifies every listed table, logs and caches; a restore drill re-applies tombstones; export contains only the user's own data.
- **Secrets:** repository and installer scans find no secret key, no `.env`; secret key appears only in backend configuration; rotation drill works; client logs and Jarvis memory contain no tokens or passwords.
- **Local-first:** local chat and inference work with no network and no account; no code path sends prompts to a hosted model; no app-created message quota exists; the adult-style restriction remains in force.
- **Operational:** free-tier pause and quota behaviour observed in staging; email limits understood; outage behaviour verified (features fail closed, local use unaffected).
- **Legal and policy:** reviewed terms, privacy notice, retention periods and age policy signed off.
Launch is blocked until every item above has a recorded pass; the pass criteria must say what was actually run.

---

## 10. Blockers and unresolved decisions
| # | Item | Recommended default |
|---|---|---|
| 1 | Do any real accounts exist on the old stdlib service? | assume no; confirm before retiring it |
| 2 | Is online chat history stored at all? | no standing server copy; evidence only from user-attached or rule-triggered snapshots |
| 3 | What online conduct can lead to a ban, given local use is invisible? | define in the terms before building the moderation tables |
| 4 | Age policy: bands, minimum age, DOB versus declaration, verification method | minimum data allowed by legal review; DOB only in the private schema |
| 5 | Appeal and retention periods (30/7/30/90 days, 14-day deletion grace, audit retention) | placeholders above, to be set with counsel |
| 6 | Ban-evasion tombstones (legal basis, hash design) | decide with counsel |
| 7 | Email: custom SMTP provider and domain | needed before registration works at any scale |
| 8 | Free versus paid plan at launch (backups, pausing, logs) | free for development only; paid before real users with appeals |
| 9 | Staff structure: who are the first admins, how many owners | at least two owners, MFA mandatory |
| 10 | Account-enabled build or optional channel | optional; builds without a server address stay account-free |
| 11 | Server-side enforcement limits for local features | accept and document; no claim of tamper-proofing |
| 12 | Verify current Supabase terms and free-tier behaviour again at launch | repeat section 7 before any launch |
| 13 | Legal review of terms, privacy notice, DSAR handling, minors, cross-border data | required before public use |

## What was and was not verified for this document
Verified by reading: repository structure, local SQLite schemas, the existing server's tables and routes, `docs/ONLINE.md`, `docs/TELEMETRY.md`, the age and personality restriction code, and the official Supabase pages listed in section 7.
Not verified: any behaviour of Supabase itself (no project exists), RLS or role designs, backups and restores, pricing beyond what the pages stated on 2026-10-28, email delivery, legal sufficiency, and anything about the old server beyond what is stated in its own docs.
