"""The set assistant (#63), with a mocked API client: the tool loop, what it sends, the settings and the keychain. No test
here touches the network or the real keychain."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError

from seedbox2.assistant import AssistantError, Config, KeyStore, Session, agent, dry_run, make_client, run
from seedbox2.assistant.config import KEY_SHOWN_AS
from seedbox2.assistant.tools import TOOLS, ToolError
from seedbox2.library import Library
from seedbox2.library.service import Settings
from seedbox2.server.app import create_app
from seedbox2.sets import TEMPLATES, OrderRequest, Problem, TrackFeatures
from seedbox2.sets.explain import plan_words, why
from seedbox2.sets.service import add_mix_plans, order_tracks

MODEL = "claude-sonnet-5-5"
SECRET = "sk-ant-test-do-not-send"


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def tool(tool_name: str, **input_) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"call_{tool_name}", name=tool_name, input=input_)


def reply(*blocks, stop="tool_use") -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=SimpleNamespace(input_tokens=100, output_tokens=20))


class FakeClient:
    """Plays a script of replies, one per request, or calls a function with the request. Keeps what was sent as JSON, at
    the moment it was sent, so the tests see exactly what would go over the wire."""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.sent: list[dict] = []
        self.messages = self

    def create(self, **request) -> SimpleNamespace:
        self.sent.append(json.loads(json.dumps(request)))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(request) if callable(step) else step


def results_of(request: dict) -> list[dict]:
    """The tool results of the last user turn, parsed."""
    return [json.loads(b["content"]) if not b.get("is_error") else b for b in request["messages"][-1]["content"]]


def seed(lib: Library, n: int = 6) -> list[str]:
    hashes = []
    for i in range(n):
        h = f"{i:040x}"
        lib.upsert_track(h, 300.0, title=f"Song {i}", artist=f"Artist {i}", album="Secret Album", source="/home/dj/Private Folder",
                         tags={"file": {"genre": "Deep House" if i < 4 else "Techno"}})  # fmt: skip
        lib.put_analysis(h, "beats", "1", {"bpm": 120.0 + i})
        lib.put_analysis(h, "key", "1", {"camelot": f"{i % 3 + 8}A", "key": "A minor", "open_key": "1m"})
        lib.put_analysis(h, "energy", "1", {"energy": i / 9, "rating": i + 1})
        lib.put_analysis(h, "vocals", "1", {"active": "1111" if i == 5 else "0000"})
        hashes.append(h)
    return hashes


@pytest.fixture
def lib(tmp_path):
    library = Library.open(tmp_path / "lib.sqlite3")
    seed(library)
    yield library
    library.close()


def search_then_choose(n: int = 4) -> list:
    """A model that searches, picks the first `n`, shapes the set, solves and finishes."""

    def choose(request):
        ids = [t["id"] for t in results_of(request)[0]["tracks"]][:n]
        return reply(tool("choose_tracks", add=ids), tool("set_energy_arc", points=[[0, 0.1], [0.67, 1], [1, 0.3]]))

    return [
        reply(text("Let me look."), tool("search_library", genre="deep")),
        choose,
        reply(tool("run_solver")),
        reply(tool("finish", name="Sunset deep", reasoning="Mellow first, peak two thirds in.")),
    ]


# --- the loop --------------------------------------------------------------------------------------------------------


def test_a_run_makes_a_draft_that_saves_nothing(lib):
    client = FakeClient(*search_then_choose())
    before = lib.sets()
    steps: list[str] = []
    draft = run(client, MODEL, "90 minutes of sunset deep house", Session(lib), on_step=steps.append)
    assert draft["name"] == "Sunset deep" and draft["reasoning"].startswith("Mellow")
    assert len(draft["result"]["order"]) == 4 and len(draft["result"]["transitions"]) == 3
    assert draft["result"]["settings"]["arc"] == [[0, 0.1], [0.67, 1], [1, 0.3]]
    assert draft["result"]["settings"]["note"] == draft["reasoning"]
    assert lib.sets() == before, "a draft is only kept when the person keeps it"
    assert draft["usage"] == {"input_tokens": 400, "output_tokens": 80}
    assert steps[0].startswith("Searching deep") and steps == draft["steps"]
    assert client.sent[0]["model"] == MODEL and {t["name"] for t in client.sent[0]["tools"]} == {t["name"] for t in TOOLS}


def test_tool_results_answer_every_call_and_follow_the_conversation(lib):
    client = FakeClient(*search_then_choose())
    run(client, MODEL, "a set", Session(lib))
    second = client.sent[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[1]["content"][0] == {"type": "text", "text": "Let me look."}
    assert second[2]["content"][0]["tool_use_id"] == second[1]["content"][1]["id"]
    third = client.sent[2]["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in third] == ["call_choose_tracks", "call_set_energy_arc"], "both calls of a turn get a result"


def test_a_bad_call_goes_back_to_the_model_to_fix(lib):
    def fixed(request):
        result = request["messages"][-1]["content"][0]
        assert result["is_error"] is True and "t99" in result["content"]
        return reply(text("No tracks for that, sorry."), stop="end_turn")

    client = FakeClient(reply(tool("choose_tracks", add=["t99"])), fixed)
    with pytest.raises(AssistantError, match="No tracks for that"):
        run(client, MODEL, "a set", Session(lib))


@pytest.mark.parametrize("bad", [
    ("nonsense_tool", {}),
    ("set_energy_arc", {}),
    ("set_energy_arc", {"template": "nope"}),
    ("set_energy_arc", {"points": [[0, 5], [1, 1]]}),
    ("set_tempo_journey", {"start_bpm": 120}),
    ("set_tempo_journey", {"start_bpm": 1000, "end_bpm": 120}),
    ("lock_track", {"id": "t1", "position": 1}),
    ("run_solver", {}),
    ("finish", {"name": "x", "reasoning": "y"}),
])  # fmt: skip
def test_tools_refuse_what_cant_be_done(lib, bad):
    session = Session(lib)
    session.search_library({"limit": 1})
    with pytest.raises(ToolError):
        session.call(*bad)


def test_it_stops_at_the_limit_of_turns_with_what_it_has(lib):
    session = Session(lib)
    ids = [t["id"] for t in session.search_library({})["tracks"]]
    session.choose_tracks({"add": ids[:3]})
    client = FakeClient(*[reply(tool("run_solver"))] * agent.MAX_TURNS)
    draft = run(client, MODEL, "a set", session)
    assert len(client.sent) == agent.MAX_TURNS and len(draft["result"]["order"]) == 3
    assert "limit" in draft["reasoning"]


def test_it_gives_up_at_the_limit_with_nothing_chosen(lib):
    client = FakeClient(*[reply(tool("search_library"))] * agent.MAX_TURNS)
    with pytest.raises(AssistantError, match="ran out of steps"):
        run(client, MODEL, "a set", Session(lib))


def test_ending_without_finish_still_gives_the_draft_it_built(lib):
    session = Session(lib)
    ids = [t["id"] for t in session.search_library({})["tracks"]]
    session.choose_tracks({"add": ids[:3]})
    draft = run(FakeClient(reply(text("Here it is."), stop="end_turn")), MODEL, "a set", session)
    assert draft["reasoning"] == "Here it is." and draft["result"]["order"]


def test_a_refusal_is_said_plainly(lib):
    with pytest.raises(AssistantError, match="declined"):
        run(FakeClient(reply(text(""), stop="refusal")), MODEL, "a set", Session(lib))


def test_it_stops_when_asked(lib):
    client = FakeClient(reply(tool("search_library")))
    with pytest.raises(AssistantError, match="Stopped") as e:
        run(client, MODEL, "a set", Session(lib), cancelled=lambda: True)
    assert e.value.status == 409 and client.sent == []


def test_empty_requests_cost_nothing(lib, tmp_path):
    client = FakeClient()
    with pytest.raises(AssistantError):
        run(client, MODEL, "  ", Session(lib))
    with pytest.raises(AssistantError):
        run(client, MODEL, "x" * (agent.MAX_PROMPT + 1), Session(lib))
    with pytest.raises(AssistantError, match="no tracks"):
        run(client, MODEL, "a set", Session(lib, []))
    assert client.sent == []


REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(("error", "status", "words"), [
    (anthropic.AuthenticationError("no", response=httpx.Response(401, request=REQUEST), body=None), 401, "rejected"),
    (anthropic.RateLimitError("no", response=httpx.Response(429, request=REQUEST), body=None), 429, "rate limit"),
    (anthropic.NotFoundError("no", response=httpx.Response(404, request=REQUEST), body=None), 502, "model"),
    (anthropic.InternalServerError("no", response=httpx.Response(500, request=REQUEST), body=None), 502, "500"),
    (anthropic.APIConnectionError(request=REQUEST), 503, "network"),
])  # fmt: skip
def test_api_errors_become_words(lib, error, status, words):
    with pytest.raises(AssistantError, match=words) as e:
        run(FakeClient(error), MODEL, "a set", Session(lib))
    assert e.value.status == status


def test_the_real_sdk_speaks_this_shape(lib):
    """The real client over a fake transport, as the API answers: the request it sends and the replies it parses are the
    ones the loop expects, not just the ones the fake above happens to have."""
    seen: list[dict] = []
    headers: list[dict] = []
    answers = [
        {"content": [{"type": "text", "text": "Looking."}, {"type": "tool_use", "id": "toolu_1", "name": "search_library", "input": {}}], "stop_reason": "tool_use"},
        {"content": [{"type": "tool_use", "id": "toolu_2", "name": "choose_tracks", "input": {"add": ["t1", "t2", "t3"]}}], "stop_reason": "tool_use"},
        {"content": [{"type": "tool_use", "id": "toolu_3", "name": "finish", "input": {"name": "N", "reasoning": "R"}}], "stop_reason": "tool_use"},
    ]  # fmt: skip

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        headers.append(dict(request.headers))
        body = {"id": "msg_1", "type": "message", "role": "assistant", "model": MODEL, "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 3}, **answers.pop(0)}  # fmt: skip
        return httpx.Response(200, json=body)

    client = make_client(SECRET, http_client=httpx.Client(transport=httpx.MockTransport(api)))
    draft = run(client, MODEL, "a set", Session(lib))
    assert draft["name"] == "N" and len(draft["result"]["order"]) == 3 and draft["usage"] == {"input_tokens": 21, "output_tokens": 9}
    assert all(SECRET in h["authorization"] for h in headers) and seen[0]["max_tokens"] == agent.MAX_TOKENS and seen[0]["system"] == agent.SYSTEM
    assert seen[1]["messages"][1]["content"][1] == {"type": "tool_use", "id": "toolu_1", "name": "search_library", "input": {}}
    assert SECRET not in json.dumps(seen), "the key goes in the header only"
    told = {k for k in headers[0] if k in ("x-stainless-os", "x-stainless-arch", "x-stainless-runtime-version", "x-stainless-lang")}
    assert not told, "nothing about this computer"
    shown = dry_run(MODEL, "a set", Session(lib))["headers"]
    sent = {k: v for k, v in headers[0].items() if k not in ("host", "content-length")}
    assert set(shown) == set(sent) and shown["authorization"] == KEY_SHOWN_AS, "the dry run shows the real headers, but the key"


def test_the_real_client_is_made_with_the_key_and_limits():
    client = make_client(SECRET)
    assert client.api_key == SECRET and client.max_retries > 0 and client.timeout


# --- the tools -------------------------------------------------------------------------------------------------------


def titles(found: dict) -> list[str]:
    return [t["title"] for t in found["tracks"]]


def test_search_filters_on_our_analysis(lib):
    s = Session(lib)
    assert titles(s.search_library({"bpm_min": 123, "bpm_max": 124})) == ["Song 3", "Song 4"]
    assert titles(s.search_library({"energy_min": 5})) == ["Song 4", "Song 5"]
    assert titles(s.search_library({"keys": ["8A"]})) == ["Song 0", "Song 3"]
    assert titles(s.search_library({"vocals_min": 0.5})) == ["Song 5"]
    assert titles(s.search_library({"genre": "techno"})) == ["Song 4", "Song 5"]
    assert titles(s.search_library({"query": "artist 2"})) == ["Song 2"]
    found = s.search_library({"limit": 2})
    assert found["matched"] == 6 and len(found["tracks"]) == 2


def test_a_tags_guess_isnt_our_analysis(tmp_path):
    lib = Library.open(tmp_path / "l.sqlite3")
    lib.upsert_track("a" * 40, 200.0, title="Tagged", tags={"file": {"bpm": 128, "key": "8A"}, "tunebat": {"energy": 7}})
    s = Session(lib)
    track = s.search_library({})["tracks"][0]
    assert "bpm" not in track and "key" not in track and "energy" not in track
    assert s.search_library({"bpm_min": 100})["matched"] == 0
    lib.close()


def test_scope_holds(lib):
    hashes = [f"{i:040x}" for i in range(2)]
    s = Session(lib, hashes)
    assert titles(s.search_library({})) == ["Song 0", "Song 1"]
    assert s.scope_summary()["tracks"] == 2


def test_vibe_text_ranks_with_our_index_and_never_sends_vectors(lib):
    sims = {f"{i:040x}": i / 10 for i in range(5)}  # the sixth has no embeddings yet
    asked = []
    s = Session(lib, vibe=lambda t: (asked.append(t), sims)[1])
    found = s.search_library({"text": "warm", "limit": 2})
    assert asked == ["warm"] and titles(found) == ["Song 4", "Song 3"] and found["matched"] == 5
    assert found["tracks"][0]["vibe"] == 0.4


def test_a_missing_clap_model_leaves_the_other_filters(lib):
    def broken(_text):
        raise RuntimeError("/home/dj/Private Folder/model.onnx is missing")

    found = Session(lib, vibe=broken).search_library({"text": "warm", "bpm_max": 120.5})
    assert titles(found) == ["Song 0"] and "isn't available" in found["note"]
    assert "Private" not in json.dumps(found), "the error may name a path, so it stays here"


def test_locks_and_the_last_position_follow_the_set(lib):
    s = Session(lib)
    ids = {t["title"]: t["id"] for t in s.search_library({})["tracks"]}
    s.choose_tracks({"add": [ids["Song 0"], ids["Song 1"], ids["Song 5"]]})
    s.lock_track({"id": ids["Song 5"], "position": -1})
    s.lock_track({"id": ids["Song 1"], "position": 1})
    s.choose_tracks({"add": [ids["Song 2"], ids["Song 3"]]})
    order = [t["title"] for t in s.solve()["order"]]
    assert order[0] == "Song 1" and order[-1] == "Song 5", "the closer stays last as the set grows"
    s.lock_track({"id": ids["Song 1"]})
    assert s.locks == {f"{5:040x}": -1}
    s.choose_tracks({"remove": [ids["Song 5"]]})
    assert s.locks == {}, "a track taken out takes its lock with it"


def test_solver_result_is_read_by_the_model_and_finish_re_solves_if_stale(lib):
    s = Session(lib)
    ids = [t["id"] for t in s.search_library({})["tracks"]]
    s.choose_tracks({"add": ids[:3]})
    out = s.call("run_solver", {})
    assert out["total_minutes"] == 15 and out["order"][0]["position"] == 1 and "target_energy" in out["order"][0]
    assert {"after_position", "text", "flags"} <= set(out["transitions"][0])
    s.choose_tracks({"add": ids[3:5]})
    s.finish({"name": "n", "reasoning": "r"})
    assert len(s.result["order"]) == 5, "the draft is what the tools last said, not the last solve"


# --- what goes out ---------------------------------------------------------------------------------------------------


def test_nothing_leaves_but_titles_artists_tags_and_numbers(lib):
    """Everything sent in a whole run, as text: no hash, path, album, audio or key."""
    client = FakeClient(*search_then_choose())
    draft = run(client, MODEL, "a set", Session(lib))
    wire = json.dumps(client.sent)
    for h in (f"{i:040x}" for i in range(6)):
        assert h not in wire
    assert "Private Folder" not in wire and ".mp3" not in wire and "Secret Album" not in wire and SECRET not in wire
    assert "Song 0" in wire and "Artist 0" in wire
    assert draft["result"]["order"][0]["hash"], "the draft comes back with real tracks"


def test_a_track_shows_only_the_allowed_fields(lib):
    s = Session(lib, vibe=lambda _t: {f"{i:040x}": 0.5 for i in range(6)})
    track = s.search_library({"text": "x"})["tracks"][0]
    assert set(track) == {"id", "title", "artist", "minutes", "bpm", "key", "energy", "vocals", "genre", "vibe"}
    assert track["id"] == "t1", "ids are the run's own, not the library's"


def test_the_dry_run_is_what_a_run_sends_first(lib):
    shown = dry_run(MODEL, "a sunset set", Session(lib))
    client = FakeClient(*search_then_choose())
    run(client, MODEL, "a sunset set", Session(lib))
    assert json.loads(json.dumps(shown["body"])) == client.sent[0]
    assert shown["url"] == "https://api.anthropic.com/v1/messages" and shown["headers"]["authorization"] == KEY_SHOWN_AS
    assert [t["title"] for t in shown["example_search_result"]["tracks"]][:2] == ["Song 0", "Song 1"]
    wire = json.dumps(shown)
    assert f"{0:040x}" not in wire and "Private Folder" not in wire


def test_the_system_prompt_treats_track_text_as_data():
    assert "not instructions" in agent.SYSTEM


# --- settings and the key --------------------------------------------------------------------------------------------


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.store: dict = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self.store:
            raise PasswordDeleteError("not there")
        del self.store[(service, username)]


class BrokenKeyring(MemoryKeyring):
    def set_password(self, service, username, password):
        raise KeyringError(f"locked: {password}")

    def get_password(self, service, username):
        raise KeyringError("locked")


@pytest.fixture
def keychain(monkeypatch):
    import keyring

    backend = MemoryKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    return backend


def test_off_by_default_with_the_default_model(tmp_path, keychain):
    c = Config(Settings(tmp_path / "settings.json"))
    assert c.status() == {"enabled": False, "model": MODEL, "models": list(c.status()["models"]), "has_key": False, "keychain": True}
    with pytest.raises(AssistantError, match="off") as e:
        c.require_key()
    assert e.value.status == 409


def test_the_key_lives_only_in_the_keychain(tmp_path, keychain):
    path = tmp_path / "settings.json"
    c = Config(Settings(path))
    c.update(enabled=True)
    with pytest.raises(AssistantError, match="API key"):
        c.require_key()
    status = c.set_key(f"  {SECRET}\n")
    assert status["has_key"] and SECRET not in json.dumps(status)
    assert list(keychain.store.values()) == [SECRET]
    assert SECRET not in path.read_text(), "not in settings.json"
    assert c.require_key() == SECRET
    assert not c.delete_key()["has_key"] and keychain.store == {}
    c.delete_key()  # again: already gone


def test_opening_settings_doesnt_read_the_keychain(tmp_path, keychain, monkeypatch):
    import keyring

    c = Config(Settings(tmp_path / "settings.json"))
    c.set_key(SECRET)
    monkeypatch.setattr(keyring, "get_password", lambda *_: pytest.fail("the keychain can ask for a password"))
    assert c.status()["has_key"]


@pytest.mark.parametrize("bad", ["", "   ", "two words"])
def test_a_key_that_isnt_one_isnt_echoed(tmp_path, keychain, bad):
    c = Config(Settings(tmp_path / "settings.json"))
    with pytest.raises(AssistantError) as e:
        c.set_key(bad)
    assert bad.strip() == "" or bad not in str(e.value)


def test_no_keychain_means_no_key_not_a_file(tmp_path, monkeypatch):
    import keyring
    from keyring.backends.fail import Keyring as Fail

    monkeypatch.setattr(keyring, "get_keyring", Fail)
    path = tmp_path / "settings.json"
    c = Config(Settings(path))
    assert c.status()["keychain"] is False
    with pytest.raises(AssistantError, match="no keychain") as e:
        c.set_key(SECRET)
    assert e.value.status == 503 and not path.exists()


def test_a_keychain_error_doesnt_repeat_the_key(tmp_path, monkeypatch):
    import keyring

    backend = BrokenKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    with pytest.raises(AssistantError) as e:
        Config(Settings(tmp_path / "s.json")).set_key(SECRET)
    assert SECRET not in str(e.value) and e.value.__cause__ is None


def test_an_unknown_model_is_refused_and_a_stale_one_falls_back(tmp_path, keychain):
    settings = Settings(tmp_path / "settings.json")
    c = Config(settings)
    with pytest.raises(AssistantError):
        c.update(model="gpt")
    assert c.update(model="claude-haiku-5-5")["model"] == "claude-haiku-5-5"
    settings.set("assistant", {"model": "claude-retired"})
    assert c.model == MODEL


def test_the_key_store_is_the_default(tmp_path):
    assert isinstance(Config(Settings(tmp_path / "s.json")).keys, KeyStore)


# --- over HTTP -------------------------------------------------------------------------------------------------------


@pytest.fixture
def http(tmp_path, keychain):
    app = create_app(library_path=tmp_path / "lib.sqlite3", start_jobs=False)
    with TestClient(app) as c:
        c.get("/api/sets")
        seed(app.state.library)
        yield c


def use_client(http, *script) -> FakeClient:
    fake = FakeClient(*script)
    keys = []
    http.app.state.assistant_client = lambda key: (keys.append(key), fake)[1]
    fake.keys = keys
    return fake


def turn_on(http) -> None:
    assert http.post("/api/assistant/settings", json={"enabled": True}).status_code == 200
    assert http.put("/api/assistant/key", json={"key": SECRET}).status_code == 200


def test_the_server_doesnt_load_the_sdk_or_the_keychain_until_asked():
    code = "import sys, seedbox2.server.app; sys.exit(any(m in sys.modules for m in ('anthropic', 'keyring')))"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_the_app_doesnt_need_any_of_it(http):
    fake = use_client(http)
    assert http.get("/api/assistant").json()["enabled"] is False
    r = http.post("/api/assistant/run", json={"prompt": "a set"})
    assert r.status_code == 409 and "off" in r.json()["detail"] and fake.keys == []
    assert http.post("/api/assistant/settings", json={"enabled": True}).json()["enabled"] is True
    r = http.post("/api/assistant/run", json={"prompt": "a set"})
    assert r.status_code == 409 and "key" in r.json()["detail"]


def test_a_draft_over_http_becomes_an_ordinary_set(http):
    turn_on(http)
    fake = use_client(http, *search_then_choose())
    r = http.post("/api/assistant/run", json={"prompt": "sunset"})
    assert r.status_code == 200, r.text
    draft = r.json()
    assert fake.keys == [SECRET] and http.get("/api/sets").json() == []
    assert SECRET not in r.text

    hashes = [t["hash"] for t in draft["result"]["order"]]
    set_id = http.post("/api/sets", json={"name": draft["name"], "hashes": hashes}).json()["id"]
    saved = http.put(f"/api/sets/{set_id}/order", json={"hashes": hashes, **draft["result"]["settings"]})
    assert saved.status_code == 200, saved.text
    again = http.get(f"/api/sets/{set_id}/order").json()
    assert [t["hash"] for t in again["order"]] == hashes
    assert again["settings"]["note"] == draft["reasoning"], "the side note stays with the set"
    assert again["settings"]["arc"] == draft["result"]["settings"]["arc"]

    # and it's an ordinary set: it orders and saves like any other, and the note can go
    assert http.post(f"/api/sets/{set_id}/order", json={"arc": "flat"}).status_code == 200
    http.put(f"/api/sets/{set_id}/order", json={"hashes": hashes, "arc": "flat"})
    assert "note" not in http.get(f"/api/sets/{set_id}/order").json()["settings"]


def test_a_set_without_a_note_looks_as_before(http):
    hashes = [f"{i:040x}" for i in range(3)]
    set_id = http.post("/api/sets", json={"name": "s", "hashes": hashes}).json()["id"]
    http.put(f"/api/sets/{set_id}/order", json={"hashes": hashes, "arc": "flat"})
    assert http.get(f"/api/sets/{set_id}/order").json()["settings"] == {"arc": "flat", "tempo_start": None, "tempo_end": None, "pins": []}


def test_a_run_stays_in_its_scope(http):
    turn_on(http)
    lib = http.app.state.library
    crate = lib.save_crate("Deep", [f"{i:040x}" for i in range(2)])
    set_id = http.post("/api/sets", json={"name": "s", "hashes": [f"{5:040x}"]}).json()["id"]

    def check(request):
        assert request["messages"][0]["content"].count('"tracks": 3') == 1
        return reply(tool("search_library"))

    def seen(request):
        assert titles(results_of(request)[0]) == ["Song 0", "Song 1", "Song 5"]
        return reply(text("Too few for this."), stop="end_turn")

    use_client(http, check, seen)
    r = http.post("/api/assistant/run", json={"prompt": "x", "set_id": set_id, "crate_ids": [crate]})
    assert r.status_code == 422 and "Too few" in r.json()["detail"]
    assert http.post("/api/assistant/run", json={"prompt": "x", "set_id": 999}).status_code == 404
    assert http.post("/api/assistant/run", json={"prompt": "x", "crate_ids": [999]}).status_code == 404


def test_crate_names_arent_sent(http):
    turn_on(http)
    crate = http.app.state.library.save_crate("My Secret Crate", [f"{i:040x}" for i in range(3)])
    fake = use_client(http, *search_then_choose(3))
    assert http.post("/api/assistant/run", json={"prompt": "x", "crate_ids": [crate]}).status_code == 200
    assert "My Secret Crate" not in json.dumps(fake.sent)


def test_one_run_at_a_time_and_steps_as_events(http):
    turn_on(http)
    from seedbox2.server.routers import assistant as router

    published = []
    events = http.app.state.events
    original = events.publish
    events.publish = lambda t, d=None, **kw: (published.append((t, d)), original(t, d, **kw))[1]
    use_client(http, *search_then_choose())
    assert http.post("/api/assistant/run", json={"prompt": "x"}).status_code == 200
    assert published[0][0] == "assistant.step" and "Searching" in published[0][1]["text"]

    assert router._running.acquire(blocking=False)
    try:
        assert http.post("/api/assistant/run", json={"prompt": "x"}).status_code == 409
        assert http.post("/api/assistant/cancel").json() == {"stopping": True}
    finally:
        router._running.release()
    assert http.post("/api/assistant/cancel").json() == {"stopping": False}


def test_key_over_http_never_comes_back(http, keychain):
    r = http.put("/api/assistant/key", json={"key": SECRET})
    assert r.status_code == 200 and SECRET not in r.text and r.json()["has_key"]
    assert SECRET not in http.get("/api/assistant").text
    bad = http.put("/api/assistant/key", json={"key": "not a key"})
    assert bad.status_code == 422 and "not a key" not in bad.text
    assert http.delete("/api/assistant/key").json()["has_key"] is False and keychain.store == {}


def test_dry_run_over_http_needs_no_key_and_no_network(http):
    fake = use_client(http)
    r = http.post("/api/assistant/dry-run", json={"prompt": "a set"})
    assert r.status_code == 200 and r.json()["body"]["messages"][0]["content"].startswith("a set")
    assert fake.keys == [] and fake.sent == []
    assert SECRET not in r.text


# --- why this transition ---------------------------------------------------------------------------------------------


def _why_problem(**second):
    a = TrackFeatures(hash="a", title="A", bpm=124.0, camelot="8A", energy=2 / 9)
    b = TrackFeatures(hash="b", title="B", **{"bpm": 125.0, "camelot": "9A", "energy": 6 / 9, **second})
    return order_tracks([a, b], OrderRequest(arc="warm_up"))


def test_why_says_it_in_plain_sentences():
    t = _why_problem()["transitions"][0]
    said = " ".join(t["why"])
    assert "next door on the Camelot wheel" in said
    assert "124 to 125 BPM" in said and "won't be heard" in said
    assert "Energy goes up from 3 to 7" in said


def test_why_tells_one_step_on_the_wheel_from_two():
    assert "two steps" in " ".join(_why_problem(camelot="10A")["transitions"][0]["why"])


def test_why_owns_up_to_a_shift_and_a_jump():
    said = " ".join(_why_problem(camelot="2B", bpm=140.0)["transitions"][0]["why"])
    assert "semitone" in said and "big jump" in said


def test_why_says_what_it_doesnt_know():
    said = " ".join(_why_problem(camelot=None, bpm=None, energy=None)["transitions"][0]["why"])
    assert "key of one of these tracks isn't analysed" in said
    assert "tempo of one" in said and "energy of one" in said


def test_why_names_the_shift_and_the_biggest_compromise():
    p = Problem([TrackFeatures(hash="a", bpm=124.0, camelot="8A", energy=0.2), TrackFeatures(hash="b", bpm=124.0, camelot="3B", energy=0.6)], TEMPLATES["flat"])
    said = why(p, [0, 1], np.array([0, 0]), 0, {"key": 3.0, "tempo": 0.1})
    assert "clash" in said[0] and said[-1] == "The biggest compromise here is the key."




def test_the_saved_mix_plan_joins_the_why(tmp_path):
    from tests.test_seedbox2_mix_plan import _plan

    lib = Library.open(tmp_path / "l.sqlite3")
    a, b = "a" * 40, "b" * 40
    for h in (a, b):
        lib.upsert_track(h, 240.0, title=h[:1])
    lib.put_plan(a, b, _plan(version=3, technique="bass swap", bars=16, edited=True), user_edited=True)
    said = add_mix_plans(lib, {"transitions": [{"from": a, "to": b, "why": []}]})["transitions"][0]["why"]
    assert said == ["The mix plan is a 16-bar bass swap, with the keys locked. You edited it by hand."]
    lib.close()


def test_plan_words_and_unreadable_plans(lib):
    assert plan_words({"not": "a plan"}, "a", "b") is None
    result = {"transitions": [{"from": "a", "to": "b", "why": ["x"]}]}
    assert add_mix_plans(lib, result)["transitions"][0]["why"] == ["x"], "no plan, nothing to add"
