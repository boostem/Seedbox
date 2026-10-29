"""seedbox2 local music import (#40): tags of every format, folders, watch folders, Rekordbox/iTunes XML, analysis
queueing, the Seedbox 1 download flow and the HTTP API. Generated files in temp folders, never the user's music."""

import base64
import json
import os
import plistlib
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest
from mutagen import id3
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from seedbox2.jobs.registry import Registry
from seedbox2.library import Library
from seedbox2.library import playlists_xml as pxml
from seedbox2.library.importer import Importer, iter_audio, queue_analysis
from seedbox2.library.service import LocalMusic, Settings
from seedbox2.library.tags import read_tags
from seedbox2.library.watch import FolderWatcher
from seedbox2.server import start_server
from seedbox2.shell import dropped_paths
from tests.test_seedbox2_library import _Client, _mp3

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

# extension -> ffmpeg codec arguments
FORMATS = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "128k"],
    "m4a": ["-c:a", "aac", "-b:a", "128k"],
    "flac": ["-c:a", "flac"],
    "wav": ["-c:a", "pcm_s16le"],
    "aiff": ["-c:a", "pcm_s16be"],
    "ogg": ["-c:a", "libvorbis"],
    "opus": ["-c:a", "libopus", "-b:a", "96k", "-ar", "48000"],
}
COVER = b"\xff\xd8\xff\xe0" + b"cover" * 200  # enough of a JPEG for tags


def _encode(path: Path, freq: int, seconds: float = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}", "-ac", "2", "-ar", "44100",
                    *FORMATS[path.suffix[1:]], str(path)], check=True)  # fmt: skip
    return path


def _tag(path: Path, title: str, artist: str, bpm: int, key: str, comment: str) -> None:
    """Write the tags the way each format's own tools do (ID3 in MP3, WAV and AIFF; MP4 atoms; Vorbis comments)."""
    ext = path.suffix[1:]
    if ext in ("mp3", "wav", "aiff"):
        if ext == "mp3":
            tags = id3.ID3()
        else:
            from mutagen.aiff import AIFF
            from mutagen.wave import WAVE

            f = (WAVE if ext == "wav" else AIFF)(path)
            f.add_tags()
            tags = f.tags
        tags.add(id3.TIT2(text=[title]))
        tags.add(id3.TPE1(text=[artist]))
        tags.add(id3.TALB(text=["Album"]))
        tags.add(id3.TBPM(text=[str(bpm)]))
        tags.add(id3.TKEY(text=[key]))
        tags.add(id3.COMM(lang="eng", desc="", text=[comment]))
        tags.add(id3.APIC(mime="image/jpeg", type=3, desc="cover", data=COVER))
        if ext == "mp3":
            tags.save(path)
        else:
            f.save()
    elif ext == "m4a":
        f = MP4(path)
        f["\xa9nam"], f["\xa9ART"], f["\xa9alb"], f["\xa9cmt"] = [title], [artist], ["Album"], [comment]
        f["tmpo"] = [bpm]
        f["----:com.apple.iTunes:initialkey"] = [MP4FreeForm(key.encode())]
        f["covr"] = [MP4Cover(COVER, imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()
    else:
        f = {"flac": FLAC, "ogg": OggVorbis, "opus": OggOpus}[ext](path)
        f["title"], f["artist"], f["album"], f["bpm"], f["initialkey"], f["comment"] = (
            title, artist, "Album", str(bpm), key, comment)  # fmt: skip
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, "image/jpeg", COVER
        if ext == "flac":
            f.add_picture(pic)
        else:
            f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode()]
        f.save()


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> dict[str, Path]:
    """One tagged file per format, each with different audio."""
    root = tmp_path_factory.mktemp("corpus")
    out = {}
    for n, ext in enumerate(FORMATS):
        path = _encode(root / f"track {ext}.{ext}", 300 + 70 * n)
        _tag(path, f"Title {ext}", f"Artist {ext}", 120 + n, "8A", f"comment {ext}")
        out[ext] = path
    return out


def _copy(corpus: dict[str, Path], dest: Path, exts=FORMATS) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    return [Path(shutil.copy2(corpus[e], dest / corpus[e].name)) for e in exts]


def _lib(tmp_path: Path) -> Library:
    return Library.open(tmp_path / "data" / "lib.sqlite3")


# --- tags --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("ext", list(FORMATS))
def test_reads_the_tags_of_every_format(corpus, ext):
    tags = read_tags(corpus[ext])
    n = list(FORMATS).index(ext)
    assert (tags.title, tags.artist, tags.album) == (f"Title {ext}", f"Artist {ext}", "Album")
    assert tags.hints["bpm"] == 120 + n
    assert tags.hints["key"] == "8A"
    assert tags.hints["comment"] == f"comment {ext}"
    assert tags.hints["format"] == ext
    assert tags.artwork == COVER and tags.artwork_mime == "image/jpeg"


def test_untagged_and_unreadable_files_give_empty_tags(tmp_path):
    bare = _encode(tmp_path / "bare.wav", 440)
    assert read_tags(bare).title == "" and read_tags(bare).hints["format"] == "wav"
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio at all")
    assert read_tags(junk).title == ""


# --- folders -----------------------------------------------------------------------------------------------------


def test_imports_a_folder_of_every_format_with_tags_as_hints(tmp_path, corpus):
    music = tmp_path / "music"
    _copy(corpus, music / "sub")
    (music / "notes.txt").write_text("not music")
    (music / ".hidden").mkdir(parents=True)
    shutil.copy2(corpus["mp3"], music / ".hidden" / "skipped.mp3")
    (music / "broken.flac").write_bytes(b"fLaC broken")
    new: list[list[str]] = []
    lib = _lib(tmp_path)
    importer = Importer(lib, on_new=new.append, artwork_dir=tmp_path / "art")
    report = importer.import_paths([music])

    assert report.files == len(FORMATS) + 1 and report.added == len(FORMATS)
    assert len(report.errors) == 1 and "broken.flac" in report.errors[0]
    assert lib.track_count() == len(FORMATS) and len(new) == 1 and len(new[0]) == len(FORMATS)
    tracks = {t["title"]: t for t in lib.tracks()}
    flac = tracks["Title flac"]
    assert flac["artist"] == "Artist flac" and flac["album"] == "Album" and flac["source"] == "local"
    assert flac["tags"]["file"]["bpm"] == 122 and flac["tags"]["file"]["key"] == "8A"
    assert Path(flac["tags"]["artwork"]).read_bytes() == COVER
    assert len(list((tmp_path / "art").iterdir())) == 1  # the same cover once
    assert 2.9 < flac["duration"] < 3.1

    again = importer.import_paths([music])  # unchanged files: no decode, nothing new
    assert again.added == 0 and again.updated == len(FORMATS) and len(new) == 1


def test_retagging_updates_the_track_and_renaming_keeps_it(tmp_path, corpus):
    music = tmp_path / "music"
    (path,) = _copy(corpus, music, ["mp3"])
    lib = _lib(tmp_path)
    importer = Importer(lib)
    importer.import_paths([music])
    (track,) = lib.tracks()
    lib.put_analysis(track["hash"], "beats", "1", {"bpm": 128})

    tags = id3.ID3(path)
    tags.add(id3.TIT2(text=["Renamed title"]))
    tags.save(path)
    moved = path.rename(music / "moved.mp3")
    report = importer.sync_folder(music)
    assert report.added == 0 and report.removed == 1
    after = lib.track(track["hash"])
    assert after["hash"] == track["hash"] and after["title"] == "Renamed title"
    assert after["paths"] == [str(moved)] and lib.analysis(track["hash"], "beats")["data"] == {"bpm": 128}

    moved.unlink()
    assert importer.sync_folder(music).removed == 1
    assert lib.track_count() == 0  # no file left and in no crate: gone


def test_a_track_in_a_crate_outlives_its_file(tmp_path, corpus):
    music = tmp_path / "music"
    (path,) = _copy(corpus, music, ["ogg"])
    lib = _lib(tmp_path)
    importer = Importer(lib)
    importer.import_paths([path])
    (track,) = lib.tracks()
    lib.save_crate("Keep", [track["hash"]])
    path.unlink()
    importer.sync_folder(music)
    assert lib.track(track["hash"])["paths"] == []


def test_iter_audio_takes_files_and_folders():
    here = Path(__file__)
    assert list(iter_audio([here, here.parent / "nope"])) == []


# --- analysis ----------------------------------------------------------------------------------------------------


def _noop(payload, ctx):
    return None


class _Jobs:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.submitted: list[tuple[str, dict, str]] = []

    def submit(self, kind, payload, *, priority):
        self.submitted.append((kind, payload, priority))
        return {"kind": kind}


def test_new_tracks_queue_light_analyses_first_and_stems_later():
    reg = Registry()
    reg.add("stems", _noop, pool="heavy", per_track=True)
    reg.add("beats", _noop, pool="light", per_track=True)
    reg.add("key", _noop, pool="light", per_track=True)
    reg.add("render", _noop, pool="heavy")  # not an analysis
    jobs = _Jobs(reg)
    queue_analysis(jobs, ["h1", "h2", "h1"])
    assert jobs.submitted == [
        ("beats", {"hash": "h1"}, "library"), ("key", {"hash": "h1"}, "library"),
        ("beats", {"hash": "h2"}, "library"), ("key", {"hash": "h2"}, "library"),
        ("stems", {"hash": "h1"}, "background"), ("stems", {"hash": "h2"}, "background"),
    ]  # fmt: skip


# --- watch folders -----------------------------------------------------------------------------------------------


def _until(cond, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_a_watch_folder_is_scanned_then_followed(tmp_path, corpus):
    music = tmp_path / "music"
    _copy(corpus, music, ["mp3", "flac"])
    lib = _lib(tmp_path)
    new: list[list[str]] = []
    watcher = FolderWatcher(Importer(lib, on_new=new.append), [music], debounce_s=0.2).start()
    try:
        assert watcher.wait_idle() and lib.track_count() == 2

        (wav,) = _copy(corpus, music / "new folder", ["wav"])
        assert _until(lambda: lib.track_count() == 3)
        h = lib.track_for_path(wav)["hash"]
        lib.put_analysis(h, "beats", "1", {})

        moved = music / "elsewhere.wav"
        wav.rename(moved)
        assert _until(lambda: lib.track_for_path(moved) is not None and lib.track_for_path(wav) is None)
        assert lib.track(h)["paths"] == [str(moved)] and lib.analysis(h, "beats") is not None

        shutil.rmtree(music / "new folder")
        moved.unlink()
        assert _until(lambda: lib.track_count() == 2)
        (music / ".partial.mp3").write_bytes(b"x")  # hidden: ignored
        assert watcher.wait_idle() and lib.track_count() == 2
        assert sum(len(n) for n in new) == 3
    finally:
        watcher.stop()


def test_downloads_into_a_seedbox1_crate_land_in_the_library_with_their_crate(tmp_path):
    """The tunebat search → crate → download flow (musicreco, unchanged) writes MP3s into a crate folder of the
    music folder, which is a watch folder: they arrive as Seedbox 1 tracks in that crate."""
    music = tmp_path / "MusicReco"
    crate = music / "Artist - Seed"
    (crate / ".seedbox").mkdir(parents=True)
    raw = {"id": "S1", "n": "Seed", "as": ["Artist"], "an": "LP", "rd": "2020", "d": 4000, "p": 50, "k": "A minor",
           "c": "8A", "b": 124.0, "e": 0.8, "da": 0.7}  # fmt: skip
    (crate / ".seedbox" / "crate.json").write_text(json.dumps({"version": 1, "seed": raw, "recs": [], "earlier": []}))
    _mp3(crate / "Artist - Seed.mp3", 440, tags={"title": "Seed", "artist": "Artist", "trck": "1/2", "spotify_id": "S1"})
    lib = _lib(tmp_path)
    watcher = FolderWatcher(Importer(lib), [music], debounce_s=0.2).start()
    try:
        assert watcher.wait_idle()
        (c,) = lib.crates()
        assert c["source"] == "seedbox1" and c["track_count"] == 1
        part = _mp3(tmp_path / "dl" / "B - Two.mp3", 550, tags={"title": "Two", "artist": "B", "trck": "2/2",
                                                                 "spotify_id": "S2"})  # fmt: skip
        shutil.move(part, crate / "B - Two.mp3")  # a finished download
        assert _until(lambda: len(lib.crate(c["id"])["tracks"]) == 2)
        two = lib.track_by_source("seedbox1", "S2")
        assert two["title"] == "Two" and [t["title"] for t in lib.crate(c["id"])["tracks"]] == ["Seed", "Two"]
    finally:
        watcher.stop()


# --- Rekordbox and iTunes XML ------------------------------------------------------------------------------------


def _url(path: Path) -> str:
    return "file://localhost" + quote(str(path))


def test_imports_rekordbox_xml_playlists_as_crates_with_cues(tmp_path, corpus):
    a, b, c = _copy(corpus, tmp_path / "rb music", ["mp3", "aiff", "m4a"])
    xml = tmp_path / "rekordbox.xml"
    xml.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
  <PRODUCT Name="rekordbox" Version="6.8.5" Company="AlphaTheta"/>
  <COLLECTION Entries="4">
    <TRACK TrackID="11" Name="A" Artist="X" AverageBpm="128.00" Tonality="Am" Comments="peak" Rating="204"
           Location="{_url(a)}">
      <TEMPO Inizio="0.000" Bpm="128.00" Metro="4/4" Battito="1"/>
      <POSITION_MARK Name="" Type="0" Start="0.000" Num="-1"/>
      <POSITION_MARK Name="Drop" Type="0" Start="60.125" Num="0"/>
      <POSITION_MARK Name="" Type="4" Start="30.000" End="37.500" Num="-1"/>
    </TRACK>
    <TRACK TrackID="12" Name="B" Artist="Y" Location="{_url(b)}"/>
    <TRACK TrackID="13" Name="C" Artist="Z" Location="{_url(c)}"/>
    <TRACK TrackID="14" Name="Gone" Location="{_url(tmp_path / 'gone.mp3')}"/>
  </COLLECTION>
  <PLAYLISTS>
    <NODE Type="0" Name="ROOT" Count="2">
      <NODE Type="0" Name="Gigs" Count="1">
        <NODE Name="Friday" Type="1" KeyType="0" Entries="3">
          <TRACK Key="12"/><TRACK Key="11"/><TRACK Key="14"/>
        </NODE>
      </NODE>
      <NODE Name="By location" Type="1" KeyType="1" Entries="1"><TRACK Key="{_url(c)}"/></NODE>
    </NODE>
  </PLAYLISTS>
</DJ_PLAYLISTS>
""")
    lib = _lib(tmp_path)
    report = pxml.import_playlists(lib, Importer(lib), xml)
    assert (report.source, report.crates, report.tracks, report.missing, report.cues) == ("rekordbox", 2, 3, 1, 1)
    crates = {c["name"]: lib.crate(c["id"]) for c in lib.crates()}
    assert set(crates) == {"Gigs / Friday", "By location"}
    friday = crates["Gigs / Friday"]
    assert friday["source"] == "rekordbox" and [t["title"] for t in friday["tracks"]] == ["Title aiff", "Title mp3"]
    ta = friday["tracks"][1]
    assert ta["tags"]["rekordbox"] == {"title": "A", "artist": "X", "bpm": 128.0, "key": "Am", "comment": "peak",
                                       "rating": 4}  # fmt: skip
    assert ta["tags"]["file"]["bpm"] == 120  # the file's own tag, beside it: both are hints
    cues = lib.analysis(ta["hash"], "rekordbox")["data"]
    assert cues["tempo"] == [{"start": 0.0, "bpm": 128.0, "meter": "4/4", "beat": 1}]
    assert [(q["start"], q["type"], q["hot_cue"]) for q in cues["cues"]] == [(0.0, "cue", None), (30.0, "loop", None),
                                                                              (60.125, "cue", 0)]  # fmt: skip
    assert cues["cues"][1]["end"] == 37.5

    pxml.import_playlists(lib, Importer(lib), xml)  # again: the same crates, updated
    assert len(lib.crates()) == 2 and lib.track_count() == 3


def test_imports_itunes_library_playlists_as_crates(tmp_path, corpus):
    a, b = _copy(corpus, tmp_path / "itunes music", ["opus", "wav"])
    plist = {
        "Major Version": 1, "Minor Version": 1, "Application Version": "1.4",
        "Tracks": {
            "100": {"Track ID": 100, "Name": "A", "Artist": "X", "BPM": 126, "Comments": "warm up", "Rating": 80,
                    "Location": "file://" + quote(str(a))},
            "101": {"Track ID": 101, "Name": "B", "Location": "file://" + quote(str(b))},
            "102": {"Track ID": 102, "Name": "Streamed"},
        },
        "Playlists": [
            {"Name": "Library", "Master": True, "Playlist ID": 1, "Playlist Items": [{"Track ID": 100}]},
            {"Name": "Music", "Distinguished Kind": 4, "Playlist ID": 2, "Playlist Items": [{"Track ID": 100}]},
            {"Name": "Sets", "Folder": True, "Playlist ID": 3, "Playlist Persistent ID": "F1"},
            {"Name": "Sunday", "Playlist ID": 4, "Playlist Persistent ID": "P1", "Parent Persistent ID": "F1",
             "Playlist Items": [{"Track ID": 101}, {"Track ID": 102}, {"Track ID": 100}]},
        ],
    }  # fmt: skip
    xml = tmp_path / "Library.xml"
    with open(xml, "wb") as f:
        plistlib.dump(plist, f)
    lib = _lib(tmp_path)
    report = pxml.import_playlists(lib, Importer(lib), xml)
    assert (report.source, report.crates, report.tracks) == ("itunes", 1, 2)
    (crate,) = lib.crates()
    full = lib.crate(crate["id"])
    assert full["name"] == "Sets / Sunday" and [t["title"] for t in full["tracks"]] == ["Title wav", "Title opus"]
    assert full["tracks"][1]["tags"]["itunes"]["comment"] == "warm up"
    assert full["tracks"][1]["source"] == "itunes"


def test_other_files_are_not_playlists(tmp_path):
    bad = tmp_path / "x.xml"
    bad.write_text("<html/>")
    with pytest.raises(pxml.NotAPlaylistFile):
        pxml.parse(bad)
    bad.write_text("not xml")
    with pytest.raises(pxml.NotAPlaylistFile):
        pxml.parse(bad)


def test_file_urls_on_each_os():
    assert pxml.file_url_to_path("file://localhost/Users/me/a%20b.mp3", "darwin") == Path("/Users/me/a b.mp3")
    assert str(pxml.file_url_to_path("file://localhost/C:/Music/x.mp3", "win32")) in ("C:\\Music\\x.mp3",
                                                                                      "C:/Music/x.mp3")  # fmt: skip
    assert pxml.file_url_to_path("https://example.com/x.mp3") is None


# --- the service, the API and the window -------------------------------------------------------------------------


def test_watch_folders_are_settings_with_the_music_folder_by_default(tmp_path, corpus):
    music = tmp_path / "music"
    music.mkdir()
    lib = _lib(tmp_path)
    service = LocalMusic(lib, settings=Settings(tmp_path / "settings.json"), music_dir=music)
    try:
        assert service.watch_folders() == [str(music)]
        other = tmp_path / "other"
        _copy(corpus, other, ["flac"])
        service.add_watch_folder(other)
        assert json.loads((tmp_path / "settings.json").read_text())["watch_folders"] == [str(music), str(other)]
        with pytest.raises(NotADirectoryError):
            service.add_watch_folder(tmp_path / "nope")
        service.remove_watch_folder(music)
        assert service.watch_folders() == [str(other)]
    finally:
        service.close()


def test_import_api(tmp_path, corpus, monkeypatch):
    music = tmp_path / "MusicReco"
    music.mkdir()
    monkeypatch.setenv("MUSICRECO_OUTPUT_ROOT", str(music))
    handle = start_server(library_path=tmp_path / "data" / "lib.sqlite3")
    client = _Client(handle.url)
    try:
        assert client.get("/api/library/folders")["folders"] == [{"path": str(music), "exists": True}]
        dropped = _copy(corpus, tmp_path / "dropped", ["mp3", "m4a"])
        status, body = client.request("POST", "/api/library/import", {"paths": [str(p) for p in dropped]})
        assert status == 202 and body["state"] == "queued"
        assert client.request("POST", "/api/library/import", {"paths": [str(tmp_path / "nope")]})[0] == 422
        assert client.request("POST", "/api/library/import/xml", {"path": str(dropped[0])})[0] == 422

        req = urllib.request.Request(handle.url + "/api/library/upload?name=" + quote("from browser.opus"),
                                     data=corpus["opus"].read_bytes(), method="POST")  # fmt: skip
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 202
        assert handle.app.state.local_music.wait_idle()
        assert client.get("/api/library")["tracks"] == 3
        assert (music / "Imported" / "from browser.opus").is_file()
        assert client.get("/api/library/import")["state"] == "done"
        assert client.request("POST", "/api/library/upload?name=notes.txt", {})[0] == 415

        other = tmp_path / "watched"
        other.mkdir()
        assert len(client.request("POST", "/api/library/folders", {"path": str(other)})[1]["folders"]) == 2
        assert client.request("POST", "/api/library/folders", {"path": str(tmp_path / "nope")})[0] == 422
        assert len(client.request("POST", "/api/library/folders/remove", {"path": str(other)})[1]["folders"]) == 1
        assert client.request("POST", "/api/library/folders/remove", {"path": str(other)})[0] == 404
        assert client.request("POST", "/api/library/choose", {"kind": "folder"})[0] == 501  # no window here
    finally:
        handle.stop()
    assert handle.app.state.local_music is None


def test_dropped_paths_come_from_pywebview_events():
    event = {"type": "drop", "dataTransfer": {"files": [{"name": "a.mp3", "pywebviewFullPath": "/m/a.mp3"},
                                                        {"name": "b.mp3"}]}}  # fmt: skip
    assert dropped_paths(event) == ["/m/a.mp3"]
    assert dropped_paths({"type": "drop"}) == []


def test_settings_survive_a_damaged_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{broken")
    s = Settings(path)
    assert s.get("watch_folders") is None
    s.set("watch_folders", ["/x"])
    assert s.get("watch_folders") == ["/x"] and os.path.exists(path)
