# Project JARVIS Online - Terms of Use and Privacy Notice

> **DRAFT TEMPLATE - NOT LEGAL ADVICE.** The service operator must replace every `[OPERATOR: ...]` placeholder and have the text
> reviewed by a lawyer for the places where users live (for example GDPR in the EU/UK, PIPEDA in Canada, CCPA in California)
> before opening the service to the public.

**Operator:** [OPERATOR: legal name]  **Contact:** [OPERATOR: contact email]  **Governing law:** [OPERATOR: jurisdiction]

## 1. What this service is
Project JARVIS Online lets signed-in users chat with an AI model run by the operator. The AI can be wrong, can make things up,
and must not be relied on for medical, legal, financial, safety or emergency decisions. If no model is connected the service says so and does not answer.

## 2. Who can use it
You must be at least 16 years old and create your own account. You need an account (username and password) before you can chat.
Keep your password and recovery code safe: there is no email reset. Without the recovery code a lost password cannot be recovered.

## 3. Learning from conversations - OFF unless you opt in
**Learning is OFF by default.** Your messages are used to improve Project JARVIS models only if you separately read and confirm the learning notice (`/v1/terms` returns it with its own fingerprint; command: `jarvis online learning on`). Creating an account does not turn it on.
- To answer you, the service must receive your messages and keep your conversation history while your account exists. That is not training use. Use `jarvis online delete-chats` to delete the history, or `delete-account` to delete everything.
- Only messages sent AFTER you opted in can be used. Turning learning off stops future use and, by default, withdraws not-yet-used earlier messages.
- Limits: text already used in a training batch cannot be removed from a model that was already trained.

## 4. What we store
Username, a salted password hash, a salted recovery-code hash, your acceptance of these terms (version and time), your learning choice, and your conversation history while your account exists.
We do not store your IP address in the database. Sessions last 30 days unless you sign out.

## 5. Your controls
You can export all your stored data, change the learning setting, sign out, and delete your account (which deletes your conversations and profile from the live database; encrypted-at-rest backups, if any, expire after [OPERATOR: backup retention]).

## 6. Acceptable use
No unlawful use, no attempts to break, overload or probe the service, no harassment, no uploading others' private data. Abuse limits (rate limits) apply to keep the service available for everyone. We may suspend accounts that break these rules.

## 7. No warranty, limited liability
The service is provided "as is" and may change or stop at any time. [OPERATOR: liability clause appropriate to your jurisdiction.]

## 8. Changes
If these terms change you will be asked to accept the new version before you can chat again.
