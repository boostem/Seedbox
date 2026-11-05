"""The optional set assistant (#63): turns a sentence into a set through Claude's API. `config` (off by default, the API
key in the OS keychain), `tools` (what the model may do: search our analysis, shape the set, run the solver) and `agent`
(the conversation, and the dry run that shows it). The app works fully without any of this and without network.

The model never sees audio, embeddings, file paths or content hashes: see `tools.public_track`."""

from seedbox2.assistant.agent import dry_run, run
from seedbox2.assistant.config import AssistantError, Config, KeyStore, make_client
from seedbox2.assistant.tools import Session

__all__ = ["AssistantError", "Config", "KeyStore", "Session", "dry_run", "make_client", "run"]
