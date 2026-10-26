"""Interactive sign-up / sign-in flows, with injectable input so they can be tested. Console wording is the consent UI."""
from __future__ import annotations

from typing import Callable

from jarvis.online.client import OnlineClient, ServiceError


def register_flow(client: OnlineClient, ask: Callable[[str], str], ask_secret: Callable[[str], str], say: Callable[[str], None]) -> bool:
    t = client.terms()
    say(t["text"])
    say(f"\n(Terms version fingerprint: {t['sha256'][:16]})")
    say("\nLearning from your conversations is OFF by default. You will be asked about it separately below; declining changes nothing.")
    if ask("Type  I accept  to agree to the terms above: ").strip().lower() != "i accept":
        say("Not accepted. No account was created.")
        return False
    if ask("Are you at least 16 years old? (yes/no): ").strip().lower() != "yes":
        say("You must be at least 16 to use this service. No account was created.")
        return False
    learning = False
    consent_sha = None
    say("\n" + t.get("learning_notice", "(no learning notice offered by this server; learning stays OFF)"))
    if t.get("learning_notice_sha256"):
        if ask("To allow training use of your future conversations type exactly  I consent  (anything else keeps learning OFF): ").strip() == "I consent":
            learning, consent_sha = True, t["learning_notice_sha256"]
    username = ask("Choose a username (3-32 letters, digits, . _ -): ").strip()
    pw = ask_secret("Choose a password (at least 10 characters): ")
    if pw != ask_secret("Repeat the password: "):
        say("The passwords did not match. No account was created.")
        return False
    try:
        r = client.register(username, pw, t["sha256"], True, learning, consent_sha)
    except ServiceError as e:
        say(f"Could not create the account: {e.message}")
        return False
    say(f"\nAccount created and signed in. Learning from conversations is {'ON' if r['learning_opt_in'] else 'OFF'}.")
    say(f"\nYOUR RECOVERY CODE (shown once, write it down): {r['recovery_code']}")
    say("Without it a forgotten password cannot be recovered.")
    return True


def login_flow(client: OnlineClient, ask, ask_secret, say) -> bool:
    try:
        r = client.login(ask("Username: ").strip(), ask_secret("Password: "))
    except ServiceError as e:
        say(f"Sign-in failed: {e.message}")
        return False
    say("Signed in.")
    try:
        if client.me().get("learning_consent_needed"):
            say("Your account had learning switched on by an older default, which is not consent. It is PAUSED: nothing of yours "
                "is used for training. To allow it, run  jarvis online learning on  and confirm the notice. To remove the old "
                "setting, run  jarvis online learning off.")
    except ServiceError:
        pass
    if r.get("needs_terms_acceptance"):
        say("The terms changed. Run: jarvis online accept-terms")
    return True
