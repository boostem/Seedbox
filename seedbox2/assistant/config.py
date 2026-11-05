"""The assistant's settings and API key. Off by default. The key goes in the OS keychain through `keyring` and nowhere
else: if there's no keychain it isn't kept at all, and no answer from the server ever contains it."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

SETTINGS_KEY = "assistant"
KEYCHAIN_SERVICE = "Seedbox 2"
KEYCHAIN_USER = "anthropic-api-key"
DEFAULT_MODEL = "claude-sonnet-5-5"
MODELS = (
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5 (balanced)"},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5 (most capable, costs more)"},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5 (fastest, costs less)"},
)
KEY_SHOWN_AS = "(your key, from the keychain)"
_PLATFORM_HEADERS = ("lang", "os", "arch", "runtime", "runtime-version", "package-version", "async")
REQUEST_TIMEOUT_S = 90.0
MAX_RETRIES = 2


class AssistantError(Exception):
    """Something to tell the person, with the HTTP status that fits."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class KeyStore:
    """The API key in the OS keychain. `keyring` backends raise all sorts (D-Bus, Security framework), so anything they
    raise means the keychain didn't work; the message never includes the key."""

    def available(self) -> bool:
        try:
            import keyring

            return keyring.get_keyring().priority > 0  # the fail and null backends have 0
        except Exception as e:
            log.info("no keychain: %s", e)
            return False

    def get(self) -> str | None:
        import keyring

        try:
            return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USER)
        except Exception as e:
            raise AssistantError(f"Couldn't read the API key from the keychain: {type(e).__name__}", 503) from None

    def set(self, key: str) -> None:
        import keyring

        try:
            keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USER, key)
        except Exception as e:
            raise AssistantError(f"Couldn't save the API key in the keychain: {type(e).__name__}", 503) from None

    def delete(self) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_USER)
        except PasswordDeleteError:
            pass  # already gone
        except Exception as e:
            raise AssistantError(f"Couldn't remove the API key from the keychain: {type(e).__name__}", 503) from None


class Config:
    """`assistant` in settings.json: `{enabled, model, has_key}`. `has_key` is a note of what we stored, so that opening
    Settings doesn't touch the keychain, which can ask for a password."""

    def __init__(self, settings: Any, keys: KeyStore | None = None) -> None:
        self.settings = settings
        self.keys = keys or KeyStore()

    def _raw(self) -> dict:
        raw = self.settings.get(SETTINGS_KEY, {})
        return raw if isinstance(raw, dict) else {}

    @property
    def enabled(self) -> bool:
        return self._raw().get("enabled") is True

    @property
    def model(self) -> str:
        model = self._raw().get("model")
        return model if any(m["id"] == model for m in MODELS) else DEFAULT_MODEL

    @property
    def has_key(self) -> bool:
        return self._raw().get("has_key") is True

    def status(self) -> dict:
        return {"enabled": self.enabled, "model": self.model, "models": list(MODELS), "has_key": self.has_key,
                "keychain": self.keys.available()}  # fmt: skip

    def _save(self, **changes: Any) -> None:
        self.settings.set(SETTINGS_KEY, {"enabled": self.enabled, "model": self.model, "has_key": self.has_key, **changes})

    def update(self, *, enabled: bool | None = None, model: str | None = None) -> dict:
        if model is not None and not any(m["id"] == model for m in MODELS):
            raise AssistantError(f"no model called {model}")
        changes = {k: v for k, v in (("enabled", enabled), ("model", model)) if v is not None}
        self._save(**changes)
        return self.status()

    def set_key(self, key: str) -> dict:
        key = key.strip()
        if not key or any(c.isspace() for c in key):
            raise AssistantError("That doesn't look like an API key")  # never echo it
        if not self.keys.available():
            raise AssistantError("This computer has no keychain to keep the key in, and it won't be kept anywhere else", 503)
        self.keys.set(key)
        self._save(has_key=True)
        return self.status()

    def delete_key(self) -> dict:
        self.keys.delete()
        self._save(has_key=False)
        return self.status()

    def require_key(self) -> str:
        """The key to run with, or why there's no run."""
        if not self.enabled:
            raise AssistantError("The assistant is off: turn it on in Settings", 409)
        key = self.keys.get() if self.has_key else None
        if not key:
            raise AssistantError("Add your Anthropic API key in Settings first", 409)
        return key


def make_client(key: str, **kw: Any) -> Any:
    """The real API client, without the headers the SDK adds about this computer (OS, architecture, Python), since the
    privacy note says nothing else is sent. Imported here so nothing loads it unless the assistant runs."""
    try:
        import anthropic
    except ImportError:
        raise AssistantError("The anthropic package isn't installed (pip install -r requirements.txt)", 503) from None
    headers = {f"x-stainless-{h}": anthropic.Omit() for h in _PLATFORM_HEADERS}
    return anthropic.Anthropic(api_key=key, timeout=REQUEST_TIMEOUT_S, max_retries=MAX_RETRIES, default_headers=headers, **kw)


def wire_preview(body: dict) -> dict:
    """The URL and headers this request really goes out with: the real client, stopped at the last moment. The key is
    the one thing not shown. Touches neither the network nor the keychain."""
    try:
        import httpx2
    except ImportError:
        raise AssistantError("The anthropic package isn't installed (pip install -r requirements.txt)", 503) from None

    seen: dict = {}

    def stop(request: Any) -> Any:
        seen["url"] = str(request.url)
        seen["headers"] = {k: KEY_SHOWN_AS if k.lower() in ("authorization", "x-api-key") else v for k, v in request.headers.items()
                           if k.lower() not in ("host", "content-length")}  # fmt: skip
        return httpx2.Response(200, json={"id": "dry-run", "type": "message", "role": "assistant", "model": body["model"],
                                          "content": [], "stop_reason": "end_turn", "stop_sequence": None,
                                          "usage": {"input_tokens": 0, "output_tokens": 0}})  # fmt: skip

    client = make_client("dry-run", http_client=httpx2.Client(transport=httpx2.MockTransport(stop)))
    client.messages.create(**body)
    return seen


def api_error_message(e: Exception) -> tuple[str, int] | None:
    """What to say about an error from the API client, or None if it isn't one."""
    try:
        import anthropic
    except ImportError:
        return None
    if isinstance(e, anthropic.AuthenticationError):
        return "Anthropic rejected the API key: check it in Settings", 401
    if isinstance(e, anthropic.PermissionDeniedError):
        return "This API key can't use the chosen model: pick another in Settings", 403
    if isinstance(e, anthropic.NotFoundError):
        return "Anthropic doesn't know the chosen model: pick another in Settings", 502
    if isinstance(e, anthropic.RateLimitError):
        return "Anthropic's rate limit was reached: try again in a minute", 429
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach Anthropic: the assistant needs a network. Everything else works without it", 503
    if isinstance(e, anthropic.APIStatusError):
        return f"Anthropic answered with an error ({e.status_code}): try again in a minute", 502
    if isinstance(e, anthropic.APIError):
        return "Anthropic's answer couldn't be read: try again", 502
    return None
