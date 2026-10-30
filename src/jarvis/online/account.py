"""The account layer between the window and the optional Jarvis Online service. NO Qt here, so every rule is testable without a display.

Three things are kept apart on purpose:
  1. Local application access: there is none to lose. Jarvis never asks for an account to start, chat locally, use memory, voice or tools.
  2. Online account sign-in: optional, only to a service address the person typed and confirmed (https, or http to THIS computer for development).
  3. Local AI: this module imports nothing from the assistant, the inference engine, the model code or the memory store (a test checks that).
Every public method returns a Result and never raises: an unreachable or misbehaving service becomes a message, never a crash, and never touches a local session."""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.net.policy import NetworkDisabled, NetworkPolicy
from jarvis.online import rules
from jarvis.online.client import FEATURE, OnlineClient, ServiceError
from jarvis.online.tokenstore import TokenStore

LOCAL_NOTE = "Your local Jarvis (chat, memory, voice, tools) works without an account and without internet. An account is only for the optional Jarvis Online service."


@dataclass
class Result:
    ok: bool
    code: str = ""
    message: str = ""
    data: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)       # field name -> problem text, for inline errors


class _Tokens:
    """Holds the session token in memory. It is written to disk ONLY when the person ticked Remember me AND the system can encrypt it
    (Windows DPAPI, tied to the Windows account). Where it cannot be encrypted, Remember me is unavailable rather than storing a readable token."""

    def __init__(self, path: Path, file_store: TokenStore | None = None):
        self.file = file_store or TokenStore(path)
        self.secure = bool(self.file.windows)
        self.remember = False
        self._mem: str | None = None

    def save(self, token: str) -> bool:
        self._mem = token
        if self.remember and self.secure:
            return self.file.save(token)
        self.file.clear()
        return True

    def load(self) -> str | None:
        return self._mem or self.file.load()

    def clear(self) -> None:
        self._mem = None
        self.file.clear()


class AccountController:
    def __init__(self, data_dir: Path, settings_path: Path, tokens: _Tokens | None = None, client_factory=None, timeout: float = 20.0):
        self.dir, self.settings_path = Path(data_dir), Path(settings_path)
        self.conf_path = self.dir / "online.json"
        self.tokens = tokens or _Tokens(self.dir / "online.token")
        self.timeout = timeout
        self._client_factory = client_factory or (lambda policy, url, toks: OnlineClient(policy, url, toks, timeout=timeout))
        self._terms: dict | None = None
        self.profile: dict = {}                      # username / display name of the signed-in person (never a password or token)
        self.expired = False

    @classmethod
    def from_config(cls, cfg) -> "AccountController":
        return cls(cfg.data_dir, cfg.settings_path)

    # ------------------------------------------------------------------ configuration (what the window shows)
    def _conf(self) -> dict:
        try:
            c = json.loads(self.conf_path.read_text(encoding="utf-8"))
            return c if isinstance(c, dict) else {}
        except (OSError, ValueError):
            return {}

    def _policy(self) -> NetworkPolicy:
        p = NetworkPolicy.load(self.settings_path)
        p.allow_insecure_localhost = bool(self._conf().get("insecure_localhost"))
        return p

    def _client(self):
        conf = self._conf()
        policy = self._policy()
        if not conf.get("url") or not policy.is_enabled(FEATURE):
            return None, policy
        return self._client_factory(policy, conf["url"], self.tokens), policy

    def state(self) -> dict:
        """Everything the window needs to draw itself. Reads two small files; makes NO network call."""
        conf = self._conf()
        connected = bool(conf.get("url")) and self._policy().is_enabled(FEATURE)
        has_token = self.tokens.load() is not None
        return {"connected": connected, "url": conf.get("url", "") if connected else "", "signed_in": connected and has_token and not self.expired,
                "expired": self.expired, "profile": dict(self.profile), "remember_available": self.tokens.secure,
                "remembered": bool(self.tokens.secure and self.tokens.file.load()), "local_note": LOCAL_NOTE}

    # ------------------------------------------------------------------ mapping errors to words a person can act on
    def _fail(self, e: Exception, doing: str) -> Result:
        if isinstance(e, NetworkDisabled):
            return Result(False, "disabled", f"{doing}: online features are off or this address is not allowed ({e}). Your local Jarvis is unaffected.")
        if isinstance(e, ServiceError):
            if e.code == "unreachable":
                return Result(False, "unreachable", f"{doing}: the service could not be reached. Check the address and your connection. Your local Jarvis still works.")
            if e.code == "timeout":
                return Result(False, "timeout", f"{doing}: the service did not answer in time. Try again later. Your local Jarvis still works.")
            if e.code == "auth_required" and self.tokens.load() is not None:
                self.tokens.clear()
                self.profile = {}
                self.expired = True
                return Result(False, "session_expired", "Your session expired or was ended elsewhere. Please sign in again. Your local Jarvis is unaffected.")
            return Result(False, e.code, e.message)
        return Result(False, "error", f"{doing}: something unexpected happened ({type(e).__name__}).")

    # ------------------------------------------------------------------ connect / disconnect (the explicit consent step)
    def connect(self, url: str, confirmed: bool, local_http: bool = False) -> Result:
        """Remember a service address and allow Jarvis to contact it. `confirmed` must be True: the window sets it only after the person read the
        consent text and pressed the confirm button. Nothing is sent by this call."""
        if not confirmed:
            return Result(False, "not_confirmed", "Not connected: the connection was not confirmed.")
        u = (url or "").strip().rstrip("/")
        parts = urllib.parse.urlsplit(u)
        host = (parts.hostname or "").lower()
        local = host in ("localhost", "127.0.0.1", "::1")
        if parts.scheme == "https" and host:
            insecure = False
        elif parts.scheme == "http" and local and local_http:
            insecure = True
        else:
            return Result(False, "bad_url", "The address must start with https://. (Plain http:// is accepted only for a service on this same computer, and only if you tick the development box.)")
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.conf_path.write_text(json.dumps({"url": u, "insecure_localhost": insecure}), encoding="utf-8")
            policy = self._policy()
            policy.enable(FEATURE)
            policy.save(self.settings_path)
        except OSError as e:
            return Result(False, "io", f"Could not save the connection settings: {e.strerror or type(e).__name__}.")
        self._terms = None
        return Result(True, message=f"Connected to {host}. Nothing has been sent; no account exists yet.")

    def disconnect(self) -> Result:
        try:
            policy = self._policy()
            policy.disable(FEATURE)
            policy.save(self.settings_path)
        except OSError:
            pass
        self.tokens.clear()
        self.profile = {}
        self.expired = False
        self._terms = None
        try:
            self.conf_path.unlink(missing_ok=True)
        except OSError:
            pass
        return Result(True, message="Disconnected. The saved sign-in was removed from this PC. An account on the service still exists until you delete it there.")

    # ------------------------------------------------------------------ terms
    def load_terms(self) -> Result:
        client, _ = self._client()
        if client is None:
            return Result(False, "not_connected", "Connect to a service first.")
        try:
            t = client.terms()
            self._terms = {"text": str(t["text"]), "sha256": str(t["sha256"])}
            return Result(True, data=dict(self._terms))
        except (ServiceError, NetworkDisabled, KeyError, TypeError) as e:
            return self._fail(e if not isinstance(e, (KeyError, TypeError)) else ServiceError(0, "bad_response", "The service sent terms in an unexpected form."), "Loading the terms")

    # ------------------------------------------------------------------ sign up / in / out
    def validate_signup(self, username: str, display_name: str, pw: str, pw2: str, accepted_terms: bool, age_ok: bool) -> dict:
        """Inline problems for the sign-up form, before anything is sent. The server checks everything again."""
        f: dict = {}
        p = rules.username_problem(username)
        if p:
            f["username"] = p
        if display_name.strip():
            p = rules.display_name_problem(display_name)
            if p:
                f["display_name"] = p
        p = rules.password_problem(pw, username)
        if p:
            f["password"] = p
        if pw != pw2:
            f["password2"] = "The two passwords do not match."
        if not accepted_terms:
            f["terms"] = "Accept the Terms and Privacy Policy to create an account."
        if not age_ok:
            f["age"] = "You must be at least 16 to create an account."
        return f

    def sign_up(self, username: str, display_name: str, pw: str, pw2: str, accepted_terms: bool, age_ok: bool, remember: bool = False) -> Result:
        problems = self.validate_signup(username, display_name, pw, pw2, accepted_terms, age_ok)
        if problems:
            return Result(False, "invalid", "Please fix the highlighted fields.", fields=problems)
        client, _ = self._client()
        if client is None:
            return Result(False, "not_connected", "Connect to a service first.")
        if self._terms is None:
            r = self.load_terms()
            if not r.ok:
                return r
        self.tokens.remember = bool(remember)
        try:
            r = client.register(rules.normalize_username(username), pw, self._terms["sha256"], True, False, None, display_name.strip() or None)
        except (ServiceError, NetworkDisabled) as e:
            res = self._fail(e, "Creating the account")
            if res.code == "username_unavailable":
                res.fields = {"username": res.message}
            elif res.code in ("bad_username", "weak_password", "bad_display_name"):
                res.fields = {{"bad_username": "username", "weak_password": "password", "bad_display_name": "display_name"}[res.code]: res.message}
            return res
        self.expired = False
        self.profile = {"username": rules.normalize_username(username), "display_name": display_name.strip() or rules.normalize_username(username)}
        return Result(True, message="Account created and signed in.", data={"recovery_code": r.get("recovery_code", "")})

    def sign_in(self, username: str, password: str, remember: bool = False) -> Result:
        if not username.strip() or not password:
            return Result(False, "invalid", "Enter your username and password.", fields={k: "Required." for k, v in (("username", username.strip()), ("password", password)) if not v})
        client, _ = self._client()
        if client is None:
            return Result(False, "not_connected", "Connect to a service first.")
        self.tokens.remember = bool(remember)
        try:
            r = client.login(rules.normalize_username(username), password)
        except (ServiceError, NetworkDisabled) as e:
            return self._fail(e, "Signing in")
        self.expired = False
        me = self.refresh()
        notes = []
        if r.get("needs_terms_acceptance"):
            notes.append("The terms changed since you signed up; online chat needs you to accept them (jarvis online accept-terms).")
        return Result(True, message="Signed in." + (" " + " ".join(notes) if notes else ""), data=me.data)

    def refresh(self) -> Result:
        """Ask the service who is signed in. Needs a network call; the window makes it only when the person opens or refreshes the Account page."""
        client, _ = self._client()
        if client is None:
            return Result(False, "not_connected", "Not connected to a service.")
        if self.tokens.load() is None:
            return Result(False, "signed_out", "Not signed in.")
        try:
            me = client.me()
        except (ServiceError, NetworkDisabled) as e:
            return self._fail(e, "Checking the session")
        self.profile = {"username": me.get("username", ""), "display_name": me.get("display_name") or me.get("username", ""),
                        "created_at": me.get("created_at"), "session_expires_at": me.get("session_expires_at")}
        self.expired = False
        return Result(True, data=dict(self.profile))

    def renew(self) -> Result:
        client, _ = self._client()
        if client is None or self.tokens.load() is None:
            return Result(False, "signed_out", "Not signed in.")
        try:
            client.renew()
        except (ServiceError, NetworkDisabled) as e:
            return self._fail(e, "Renewing the session")
        return self.refresh()

    def sign_out(self, everywhere: bool = False) -> Result:
        """Always ends the session on THIS PC. Tells the service too when it can be reached."""
        client, _ = self._client()
        told = True
        if client is not None and self.tokens.load() is not None:
            try:
                client.revoke_all_sessions() if everywhere else client.logout()
            except (ServiceError, NetworkDisabled):
                told = False
        self.tokens.clear()
        self.profile = {}
        self.expired = False
        if told:
            return Result(True, message="Signed out everywhere." if everywhere else "Signed out.")
        return Result(True, "not_told", "Signed out on this PC. The service could not be reached, so it was not told: this session stays valid there until it expires. "
                                         "Use Sign out everywhere once the service is reachable if that matters.")

    # ------------------------------------------------------------------ account management
    def set_display_name(self, name: str) -> Result:
        p = rules.display_name_problem(name)
        if p:
            return Result(False, "invalid", p, fields={"display_name": p})
        return self._authed("Changing the display name", lambda c: c.set_display_name(name.strip()), lambda r: self.profile.update(display_name=r.get("display_name", name.strip())),
                            "Display name updated.")

    def change_password(self, current: str, new: str, new2: str) -> Result:
        username = self.profile.get("username", "")
        if not current:
            return Result(False, "invalid", "Enter your current password.", fields={"current": "Required."})
        p = rules.password_problem(new, username)
        if p:
            return Result(False, "invalid", p, fields={"password": p})
        if new != new2:
            return Result(False, "invalid", "The two new passwords do not match.", fields={"password2": "The two passwords do not match."})
        return self._authed("Changing the password", lambda c: c.change_password(current, new), None, "Password changed. Your other sessions were signed out.")

    def delete_account(self, password: str) -> Result:
        if not password:
            return Result(False, "invalid", "Enter your password to confirm.", fields={"password": "Required."})
        res = self._authed("Deleting the account", lambda c: c.delete_account(password), None, "Account deleted from the service. Your local Jarvis data was not touched.")
        if res.ok:
            self.tokens.clear()
            self.profile = {}
        return res

    def recover_password(self, username: str, code: str, new: str, new2: str) -> Result:
        p = rules.password_problem(new, username)
        if p:
            return Result(False, "invalid", p, fields={"password": p})
        if new != new2:
            return Result(False, "invalid", "The two new passwords do not match.", fields={"password2": "The two passwords do not match."})
        client, _ = self._client()
        if client is None:
            return Result(False, "not_connected", "Connect to a service first.")
        try:
            r = client.recover(rules.normalize_username(username), code.strip(), new)
        except (ServiceError, NetworkDisabled) as e:
            return self._fail(e, "Resetting the password")
        return Result(True, message="Password reset. Sign in with the new password.", data={"recovery_code": r.get("recovery_code", "")})

    def _authed(self, doing, call, after, ok_message) -> Result:
        client, _ = self._client()
        if client is None or self.tokens.load() is None:
            return Result(False, "signed_out", "Sign in first.")
        try:
            r = call(client)
        except (ServiceError, NetworkDisabled) as e:
            return self._fail(e, doing)
        if after:
            after(r)
        return Result(True, message=ok_message, data=r if isinstance(r, dict) else {})
