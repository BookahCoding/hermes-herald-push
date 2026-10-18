import json
import os
import sqlite3
import stat
import sys

import pytest

from herald_push import commands, crypto, push, watcher
from herald_push.store import Device, InvalidRegistration, Store


def phone(**overrides):
    enc, sig = crypto.generate_key(), crypto.generate_key()
    data = {
        "device_id": "0b6f2c1e-1111-4222-8333-944445555666", "name": "Pixel", "server": "https://ntfy.sh",
        "push_topic": crypto.new_topic(), "reply_topic": crypto.new_topic(),
        "enc_pub": crypto.encode_public(enc.public_key()), "sig_pub": crypto.encode_public(sig.public_key()),
    }
    data.update(overrides)
    return data, enc


@pytest.mark.parametrize("bad", [
    {"server": "http://ntfy.sh"}, {"server": "https://evil.example/path"}, {"server": "https://user@host"},
    {"push_topic": "mytopic"}, {"push_topic": "hp-" + "0" * 31}, {"device_id": "../../etc"},
    {"enc_pub": "AAAA"}, {"sig_pub": 5},
])
def test_registration_fields_are_validated(bad):
    data, _ = phone(**bad)
    with pytest.raises(InvalidRegistration):
        Device.parse(data)


def test_topics_must_differ_and_cant_be_taken_over(tmp_path):
    data, _ = phone()
    with pytest.raises(InvalidRegistration):
        Device.parse({**data, "reply_topic": data["push_topic"]})
    store = Store(tmp_path)
    store.upsert(Device.parse(data))
    thief, _ = phone(device_id="99999999-aaaa-bbbb-cccc-dddddddddddd", push_topic=data["push_topic"])
    with pytest.raises(InvalidRegistration):
        store.upsert(Device.parse(thief))
    # The same phone may register again (new keys, same id).
    again, _ = phone()
    store.upsert(Device.parse(again))
    assert [d.push_topic for d in store.devices()] == [again["push_topic"]]


def test_name_is_sanitised():
    data, _ = phone(name="evil\nname\x1b[31m" + "x" * 200)
    device = Device.parse(data)
    assert "\n" not in device.name and "\x1b" not in device.name and len(device.name) <= 64


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_private_files_are_0600(tmp_path):
    store = Store(tmp_path / "data")
    store.identity()
    data, _ = phone()
    store.upsert(Device.parse(data))
    for name in ("identity.json", "devices.json"):
        assert stat.S_IMODE(os.stat(tmp_path / "data" / name).st_mode) == 0o600


def test_identity_is_stable(tmp_path):
    store = Store(tmp_path)
    assert store.public_identity() == Store(tmp_path).public_identity()


def test_commands_refuse_untrusted_surfaces(tmp_path):
    store = Store(tmp_path)
    data, _ = phone()
    handle = commands.make_handler(store, "t", lambda: True, trusted=lambda: False)
    answer = json.loads(handle("register " + json.dumps(data)))
    assert answer["ok"] is False and store.devices() == []
    assert json.loads(handle("info"))["ok"] is False


def test_commands_register_and_answer_with_gateway_keys(tmp_path):
    store = Store(tmp_path)
    data, _ = phone()
    handle = commands.make_handler(store, "t", lambda: True, trusted=lambda: True)
    answer = json.loads(handle("register " + json.dumps(data)))
    assert answer["ok"] and answer["gateway"]["sig_pub"] == store.public_identity()["sig_pub"]
    assert json.loads(handle("register {not json"))["ok"] is False
    assert json.loads(handle("unregister " + data["device_id"]))["ok"] is True
    assert store.devices() == []


def test_trusted_surface_fails_closed_outside_the_dashboard():
    assert commands.trusted_surface() is False


def test_silence_and_long_lived():
    assert watcher.is_silence(" *NO_REPLY* ")
    assert watcher.is_silence("[SILENT]")
    assert not watcher.is_silence("All done")
    # Decided by what is loaded, whatever the launcher was called.
    assert watcher.is_long_lived_process(["some-launcher", "--port", "9"], modules={"tui_gateway.server": object()})
    assert watcher.is_long_lived_process(["hermes-backend"], modules={"gateway.run": object()})
    assert watcher.is_long_lived_process(["hermes", "dashboard"], modules={})
    assert not watcher.is_long_lived_process(["hermes", "gateway", "status"], modules={})
    assert not watcher.is_long_lived_process(["hermes", "-p", "x", "chat", "-Q"], modules={"tui_gateway.server": object()})


def test_each_bots_own_switch_is_honoured(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    root = tmp_path / "hermes"
    _make_db(root, [])
    side = root / "profiles" / "side"
    _make_db(side, [])
    (side / "config.yaml").write_text("plugins:\n  entries:\n    herald-push:\n      settings:\n        enabled: false\n", encoding="utf-8")
    assert watcher.profile_enabled(side) is False
    assert watcher.profile_enabled(root) is True
    monkeypatch.setattr(watcher, "bot_chat_tip", lambda home: "s1" if (home / "state.db").exists() else None)
    sent = []
    monkeypatch.setattr(push, "publish", lambda device, payload, sig, client=None: sent.append(payload["bot"]) or True)
    store = Store(tmp_path / "data")
    store.upsert(Device.parse(phone()[0]))
    w = watcher.Watcher(root, store, now=lambda: now)
    w.poll()
    _add(root, ("s1", "assistant", "from main", None, 1, None, now))
    _add(side, ("s1", "assistant", "from side", None, 1, None, now))
    assert w.poll() == 1
    assert sent == ["default"]


def test_marks_are_saved_on_push_or_every_so_often(tmp_path, monkeypatch):
    clock = {"now": 1_800_000_000.0}
    root = tmp_path / "hermes"
    _make_db(root, [])
    monkeypatch.setattr(watcher, "bot_chat_tip", lambda home: "s1" if (home / "state.db").exists() else None)
    monkeypatch.setattr(push, "publish", lambda *a, **k: True)
    store = Store(tmp_path / "data")
    store.upsert(Device.parse(phone()[0]))
    saves = []
    monkeypatch.setattr(store, "save_marks", lambda marks: saves.append(dict(marks)))
    w = watcher.Watcher(root, store, now=lambda: clock["now"])
    w.poll()
    assert len(saves) == 1  # first sight
    for _ in range(5):
        _add(root, ("s1", "user", "typing…", None, 1, None, clock["now"]))
        clock["now"] += 3
        w.poll()
    assert len(saves) == 1  # busy turn, nothing pushed: no fsync per poll
    clock["now"] += watcher.MARKS_SAVE_SECONDS
    w.poll()
    assert len(saves) == 2  # and at most every so often
    _add(root, ("s1", "assistant", "done", None, 1, None, clock["now"]))
    assert w.poll() == 1
    assert len(saves) == 3  # a push saves at once


def _make_db(home, rows):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,"
                 " tool_calls TEXT, active INTEGER DEFAULT 1, display_kind TEXT, timestamp REAL)")
    conn.executemany("INSERT INTO messages (session_id, role, content, tool_calls, active, display_kind, timestamp) "
                     "VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _add(home, *rows):
    conn = sqlite3.connect(home / "state.db")
    conn.executemany("INSERT INTO messages (session_id, role, content, tool_calls, active, display_kind, timestamp) "
                     "VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_watcher_pushes_only_new_final_replies(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    root = tmp_path / "hermes"
    _make_db(root, [("s1", "assistant", "old news", None, 1, None, now - 60)])
    monkeypatch.setattr(watcher, "bot_chat_tip", lambda home: "s1" if (home / "state.db").exists() else None)
    sent = []
    monkeypatch.setattr(push, "publish", lambda device, payload, sig, client=None: sent.append((device, payload)) or True)
    store = Store(tmp_path / "data")
    data, enc = phone()
    store.upsert(Device.parse(data))
    w = watcher.Watcher(root, store, now=lambda: now)

    assert w.poll() == 0  # first sight: starts from now, the old reply isn't pushed
    _add(root,
         ("s1", "user", "hi", None, 1, None, now),
         ("s1", "assistant", "", '[{"id":"t"}]', 1, None, now),      # tool call
         ("s1", "assistant", "NO_REPLY", None, 1, None, now),        # silence
         ("s1", "assistant", "hidden", None, 0, None, now),          # inactive
         ("s1", "assistant", "Hello there", None, 1, None, now),
         ("s2", "assistant", "another chat", None, 1, None, now))
    assert w.poll() == 1
    assert sent[0][1]["type"] == "bot_message" and sent[0][1]["text"] == "Hello there" and sent[0][1]["bot"] == "default"
    assert w.poll() == 0  # nothing twice
    # A restart resumes from the saved mark.
    _add(root, ("s1", "assistant", "after restart", None, 1, None, now))
    w2 = watcher.Watcher(root, store, now=lambda: now)
    w2._marks = store.marks()
    assert w2.poll() == 1 and sent[-1][1]["text"] == "after restart"


def test_watcher_skips_stale_replies(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    root = tmp_path / "hermes"
    _make_db(root, [])
    monkeypatch.setattr(watcher, "bot_chat_tip", lambda home: "s1" if (home / "state.db").exists() else None)
    monkeypatch.setattr(push, "publish", lambda *a, **k: True)
    store = Store(tmp_path / "data")
    store.upsert(Device.parse(phone()[0]))
    w = watcher.Watcher(root, store, now=lambda: now)
    w.poll()
    _add(root, ("s1", "assistant", "from yesterday", None, 1, None, now - 86400))
    assert w.poll() == 0


def test_compression_copies_are_not_pushed_twice(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    root = tmp_path / "hermes"
    _make_db(root, [])
    conn = sqlite3.connect(root / "state.db")
    conn.execute("ALTER TABLE messages ADD COLUMN _compressed_summary INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()
    tip = {"id": "s1"}
    monkeypatch.setattr(watcher, "bot_chat_tip", lambda home: tip["id"] if (home / "state.db").exists() else None)
    sent = []
    monkeypatch.setattr(push, "publish", lambda device, payload, sig, client=None: sent.append(payload["text"]) or True)
    store = Store(tmp_path / "data")
    store.upsert(Device.parse(phone()[0]))
    w = watcher.Watcher(root, store, now=lambda: now)
    w.poll()
    _add(root, ("s1", "assistant", "Done with the report", None, 1, None, now))
    assert w.poll() == 1
    # Compression: a continuation carrying a summary and a copy of the last reply, then a real new reply.
    tip["id"] = "s2"
    conn = sqlite3.connect(root / "state.db")
    conn.execute("INSERT INTO messages (session_id, role, content, active, timestamp, _compressed_summary) "
                 "VALUES ('s2', 'assistant', 'Summary of earlier', 1, ?, 1)", (now,))
    conn.execute("INSERT INTO messages (session_id, role, content, active, timestamp) VALUES ('s2', 'assistant', 'Done with the report', 1, ?)", (now,))
    conn.execute("INSERT INTO messages (session_id, role, content, active, timestamp) VALUES ('s2', 'assistant', 'Something new', 1, ?)", (now,))
    conn.commit()
    conn.close()
    assert w.poll() == 1
    assert sent == ["Done with the report", "Something new"]


def test_concurrent_registrations_from_separate_stores_all_land(tmp_path):
    import threading
    phones = [phone(device_id=f"{i:08d}-0000-4000-8000-000000000000")[0] for i in range(8)]
    threads = [threading.Thread(target=lambda d=d: Store(tmp_path).upsert(Device.parse(d))) for d in phones]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(Store(tmp_path).devices()) == 8
    assert len({Store(tmp_path).public_identity()["sig_pub"] for _ in range(3)}) == 1


def test_pushed_envelope_opens_on_the_phone(tmp_path):
    store = Store(tmp_path)
    data, enc = phone()
    device = Device.parse(data)
    _, _, sig = store.identity()
    body = push.envelope_for(device, push.message("bot_message", bot="b", text="hi ✓"), sig)
    opened = crypto.open_envelope(crypto.Envelope.from_json(body), device.push_topic, crypto.G2P, enc,
                                  crypto.decode_public(store.public_identity()["sig_pub"]))
    assert json.loads(opened)["text"] == "hi ✓"
    assert len(body) <= push.MAX_BODY
    assert len(push.envelope_for(device, push.message("bot_message", text=push.clip("é" * 5000)), sig)) <= push.MAX_BODY


def test_a_ring_carries_the_call_and_fits_one_push(tmp_path):
    from herald_push import plugin
    store = Store(tmp_path)
    data, enc = phone()
    device = Device.parse(data)
    _, _, sig = store.identity()
    request = {"id": "f" * 32, "tool": "phone_call_me", "session": "20261018_abc", "expires": 1_800_000_075,
               "args": {"reason": "日本" * 150, "details": "😀" * 2000}}
    message = plugin.wake_message(request)
    assert message["type"] == "phone_call" and message["request_id"] == "f" * 32
    assert message["session"] == "20261018_abc" and message["expires"] == 1_800_000_075
    assert len(message["reason"].encode()) <= plugin.MAX_REASON_BYTES
    assert len(message["details"].encode()) <= plugin.MAX_DETAILS_BYTES
    body = push.envelope_for(device, message, sig)
    assert len(body) <= push.MAX_BODY
    opened = json.loads(crypto.open_envelope(crypto.Envelope.from_json(body), device.push_topic, crypto.G2P, enc,
                                             crypto.decode_public(store.public_identity()["sig_pub"])))
    assert opened["reason"].startswith("日本") and opened["details"].endswith("…")
    # Any other tool is only a nudge: the call is fetched over the socket.
    other = plugin.wake_message({"id": "e" * 32, "tool": "phone_location", "args": {"fresh": True}})
    assert other["type"] == "phone_request" and "args" not in other and "reason" not in other
