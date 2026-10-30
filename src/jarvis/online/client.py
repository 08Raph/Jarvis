from __future__ import annotations

import json
import socket

from jarvis.net.policy import NetworkDisabled, NetworkPolicy
from jarvis.online.tokenstore import TokenStore

FEATURE = "online_assistant"
MAX_RESPONSE = 1_000_000


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class OnlineClient:
    def __init__(self, policy: NetworkPolicy, base_url: str, tokens: TokenStore, timeout: float = 120.0):
        self.policy, self.base, self.tokens, self.timeout = policy, base_url.rstrip("/"), tokens, timeout

    def _call(self, path: str, payload: dict | None = None, auth: bool = True, method_get: bool = False):
        headers = {"Accept": "application/json", "User-Agent": "ProjectJarvis-Client"}
        data = None
        if not method_get:
            data = json.dumps(payload or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            tok = self.tokens.load()
            if not tok:
                raise ServiceError(401, "auth_required", "You are not signed in. Run: jarvis online login")
            headers["Authorization"] = "Bearer " + tok
        try:
            resp = self.policy.open(FEATURE, self.base + path, timeout=self.timeout, data=data, headers=headers)
            body = resp.read(MAX_RESPONSE + 1)
        except NetworkDisabled:
            raise
        except Exception as e:  # HTTPError carries a JSON body; anything else means the server is unreachable
            code = getattr(e, "code", None)
            if code and hasattr(e, "read"):
                try:
                    j = json.loads(e.read(MAX_RESPONSE).decode("utf-8"))
                    raise ServiceError(code, str(j.get("error", "error")), str(j.get("message", "request failed")))
                except (ValueError, UnicodeDecodeError):
                    raise ServiceError(code, "error", f"The server returned HTTP {code}.")
            reason = getattr(e, "reason", e)
            if isinstance(e, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout)):
                raise ServiceError(0, "timeout", "The service did not answer in time.")
            raise ServiceError(0, "unreachable", f"Could not reach the service: {type(e).__name__}")
        if len(body) > MAX_RESPONSE:
            raise ServiceError(0, "bad_response", "The server sent an unexpectedly large response.")
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ServiceError(0, "bad_response", "The server sent something that is not JSON.")

    # --- public API ---
    def health(self): return self._call("/v1/health", auth=False, method_get=True)
    def terms(self): return self._call("/v1/terms", auth=False, method_get=True)

    def register(self, username, password, terms_sha, age_confirmed, learning_opt_in=False, learning_consent_sha=None, display_name=None):
        body = {"username": username, "password": password, "accepted_terms_sha256": terms_sha,
                "age_confirmed": age_confirmed, "learning_opt_in": bool(learning_opt_in)}
        if display_name:
            body["display_name"] = display_name
        if learning_opt_in:
            body["learning_consent_sha256"] = learning_consent_sha
        r = self._call("/v1/register", body, auth=False)
        self.tokens.save(r.pop("token"))
        return r

    def login(self, username, password):
        r = self._call("/v1/login", {"username": username, "password": password}, auth=False)
        self.tokens.save(r.pop("token"))
        return r

    def logout(self):
        try:
            self._call("/v1/logout", {})
        finally:
            self.tokens.clear()

    def me(self): return self._call("/v1/me", {})
    def accept_terms(self, sha): return self._call("/v1/accept-terms", {"sha256": sha})
    def set_learning(self, on: bool, withdraw_past: bool = True, consent_sha: str | None = None):
        body = {"learning_opt_in": on, "withdraw_past": withdraw_past}
        if on:
            body["learning_consent_sha256"] = consent_sha
        return self._call("/v1/settings/learning", body)

    def delete_chats(self, conversation_id=None):
        body = {"conversation_id": conversation_id} if conversation_id else {"confirm": True}
        return self._call("/v1/messages/delete", body)
    def chat(self, messages, conversation_id=None):
        p = {"messages": messages}
        if conversation_id:
            p["conversation_id"] = conversation_id
        return self._call("/v1/chat", p)
    def export(self): return self._call("/v1/account/export", {})
    def delete_account(self, password):
        r = self._call("/v1/account/delete", {"password": password})
        self.tokens.clear()
        return r
    def recover(self, username, code, new_password):
        return self._call("/v1/recover", {"username": username, "recovery_code": code, "new_password": new_password}, auth=False)

    # --- account management (v2 service) ---
    def renew(self):
        r = self._call("/v1/session/renew", {})
        self.tokens.save(r.pop("token"))
        return r

    def revoke_all_sessions(self):
        try:
            return self._call("/v1/sessions/revoke-all", {})
        finally:
            self.tokens.clear()

    def set_display_name(self, name): return self._call("/v1/account/display-name", {"display_name": name})
    def change_password(self, current, new): return self._call("/v1/account/password", {"current_password": current, "new_password": new})
