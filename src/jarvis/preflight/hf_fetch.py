"""The in-app fetcher for the model-metadata check. Every connection goes through jarvis.net.policy.NetworkPolicy (the only module allowed to open
sockets): the feature must be enabled for this one action by the caller, only https://huggingface.co[/subdomain] is allowed (also after redirects), and
the call is written to the policy's audit list. Only the repository's JSON record (https://huggingface.co/api/models/<repo>) is requested: never a
`resolve/` URL, so no model bytes are transferred. The body is capped and must be a JSON object."""
from __future__ import annotations

from urllib.parse import urlsplit

from jarvis.net.policy import NETWORK_ERRORS, NetworkDisabled
from jarvis.preflight.metadata import API_HOST, ApiUnavailable, api_url, read_json_capped

FEATURE = "model_download"      # the existing, default-OFF switch for talking to the model host; the caller grants it for this one action only


def make_fetcher(policy, timeout: float = 20.0):
    def fetch(repo: str) -> dict:
        url = api_url(repo)                      # validates the repository name; builds the API URL
        try:
            with policy.open(FEATURE, url, timeout=timeout, headers={"User-Agent": "project-jarvis-readiness-check"}, allowed_host_suffixes=(API_HOST,)) as resp:
                final = urlsplit(resp.geturl())
                if final.scheme != "https" or (final.hostname or "") != API_HOST or not final.path.startswith("/api/"):
                    raise ApiUnavailable("the host redirected away from its API; the response was not used")
                return read_json_capped(resp.read)
        except ApiUnavailable:
            raise
        except NetworkDisabled as exc:
            raise ApiUnavailable(f"the app's network policy refused the request: {exc}") from None
        except NETWORK_ERRORS as exc:
            raise ApiUnavailable(f"network error ({type(exc).__name__}): the host could not be reached") from None
        except (OSError, ValueError) as exc:
            raise ApiUnavailable(f"request failed ({type(exc).__name__})") from None
    return fetch
