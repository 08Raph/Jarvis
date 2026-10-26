# Optional telemetry (diagnostics and usage data)

## What this is
The AI runs **locally** on each user's PC. Separately, a user may **choose** to share small anonymous reports with your central database so you can see
whether installs work, which hardware they run on, and which features are used. Default: **OFF**. Nothing is queued or sent until the user reads the notice
and types `I agree` (`jarvis telemetry enable`). This is the design most privacy laws expect for non-essential analytics (prior, informed, revocable consent);
it is also what keeps users' trust in a "local, private" assistant. If you want default-on for your own purposes, get legal advice first: that usually
requires a different legal basis and region-specific handling, and it would contradict the project's local-first promise.

## Exactly what can be sent (allowlist in `src/jarvis/telemetry/schema.py`)
| Event | Category | Fields |
|---|---|---|
| `app_start` | diagnostics | app version, Windows version string, RAM bucket (4/8/16/32/64 GB), CPU-thread bucket, GPU present yes/no, packaged yes/no |
| `selftest_result` | diagnostics | model id (catalog id, or `custom`), passed yes/no, load seconds, tokens/second, runtime version |
| `model_installed` | diagnostics | model id (catalog id or `custom`), source (catalog/file) |
| `error` | diagnostics | component and a fixed error code from a short list (never message text) |
| `feature_used` | usage | feature name from a fixed list, count per hour |
Every event also has: schema version, the **hour** (not exact time), and a random **install ID** (a new random number made when consent is given; not derived from hardware, name or account).

**Never sent:** chats, prompts, replies, files, paths, voice, names, e-mail, PC name, IP address (the server does not store it), exception text. There is no field that can hold free text: every field is an enum, number range, or strict pattern, and tests try to push prompts, paths and e-mail addresses through every field.

## User controls (all in the app)
    jarvis telemetry status | schema | preview | send | enable [--diagnostics-only|--usage-only] | disable | delete-my-data | reset-id
- `preview` shows the exact queued events; `send` sends exactly those (a test checks that preview equals what the server receives).
- `disable` stops recording, deletes unsent events and turns the network flag off. `delete-my-data` asks the server to delete everything for this install ID, then creates a new ID.
- If a future version adds an event or field, `NOTICE_VERSION` is bumped and telemetry stays off until the user agrees again.
- Sending happens at the end of `chat`, `selftest` and `setup` (about 4 seconds maximum) and with `jarvis telemetry send`; failures are silent and never affect the app.

## Your side: the central database
Included: the receiving endpoint in `server/jarvis_server` (`POST /v1/telemetry`, `/v1/telemetry/delete`), stored in SQLite (`telemetry` table: time received, install ID, event, event hour, app version, JSON data; **no IP, no account link**).
- Validates every event against the same schema; rejects bad ones individually; 100 events per request; rate limits per network and per install ID.
- Retention: events older than `--telemetry-retention-days` (default 180) are purged when the server starts.
- `--no-telemetry` turns the endpoint off. Report: `python -m jarvis_server.telemetry_report --db service.db` (counts, versions, RAM buckets, self-test pass/fail, error codes, average speed per model).
- SQLite is fine for a small launch. A large audience would need a server database (e.g. PostgreSQL) and a hardened web front end; **not built or load-tested**.
- Run it behind HTTPS (see ONLINE.md). The app refuses plain http except for the localhost development flag.

## Building a release that can use it
    build_installer.bat -TelemetryUrl https://telemetry.example.org
The address is written into `endpoint.txt` for the build only and reset afterwards; builds without it contain no server address, so `telemetry enable` says "nothing to enable". Edit the `[OPERATOR: ...]` placeholders in `notice_text()` (retention, legal name, legal basis) before release, and keep your published privacy policy consistent with this page.

## Honest limits
- Tested only against a local HTTP server and fake data; not run on Windows or over real HTTPS.
- Install IDs are pseudonymous, not anonymous in a strict legal sense; treat them as personal data for deletion requests.
- Anyone who learns an install ID could request its deletion (it acts like a secret); that is accepted to keep the system account-free.
- Telemetry is separate from the optional sign-in service in ONLINE.md (which has its own "learning from conversations" consent). They are not linked.
