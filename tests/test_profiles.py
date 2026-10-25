"""Which profiles a phone may be sent: the per-phone list, and every way a push or the Inbox could ignore it.

Everything here is synthetic: made-up profiles, made-up messages, a fake ``state.db``. Nothing reads a real one.
"""

import json
import os
import sqlite3
import time

import pytest

from herald_push import commands, crypto, cron, push, watcher
from herald_push.store import Allowlist, Device, InvalidRegistration, Store

ME = "0b6f2c1e-1111-4222-8333-944445555666"
OTHER = "1b6f2c1e-1111-4222-8333-944445555666"
NOW = 1_800_000_000.0


def phone(device_id=ME):
    enc, sig = crypto.generate_key(), crypto.generate_key()
    return {
        "device_id": device_id, "name": "Pixel", "server": "https://ntfy.sh",
        "push_topic": crypto.new_topic(), "reply_topic": crypto.new_topic(),
        "enc_pub": crypto.encode_public(enc.public_key()), "sig_pub": crypto.encode_public(sig.public_key()),
    }


def make_db(home, rows=()):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,"
                 " tool_calls TEXT, active INTEGER DEFAULT 1, display_kind TEXT, timestamp REAL)")
    conn.executemany("INSERT INTO messages (session_id, role, content, tool_calls, active, display_kind, timestamp) "
                     "VALUES (?,?,?,?,?,?,?)", list(rows))
    conn.commit()
    conn.close()


def say(home, text):
    conn = sqlite3.connect(home / "state.db")
    conn.execute("INSERT INTO messages (session_id, role, content, tool_calls, active, display_kind, timestamp) "
                 "VALUES ('s1','assistant',?,NULL,1,NULL,?)", (text, NOW))
    conn.commit()
    conn.close()


def agent_run(name="job", response="All good."):
    return (f"# Cron Job: {name}\n\n**Job ID:** {name}\n**Run Time:** 2026-10-12 08:02:06\n\n"
            f"## Prompt\n\nGo.\n\n## Script Output\n\n## Response\n\n{response}\n")


FAILED_RUN = "# Cron Job: job (FAILED)\n\n**Job ID:** job\n\n## Error\n\nboom\n"


def write_run(home, job, stem, doc, age=60):
    folder = home / "cron" / "output" / job
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.md"
    path.write_text(doc, encoding="utf-8")
    then = time.time() - age
    os.utime(path, (then, then))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A root with the default profile and froebel, two registered phones, and a log of what was pushed."""
    root = tmp_path / "hermes"
    make_db(root)
    make_db(root / "profiles" / "froebel")
    monkeypatch.setattr(watcher, "bot_chat_tip", lambda home: "s1" if (home / "state.db").exists() else None)
    sent = []
    monkeypatch.setattr(push, "publish", lambda device, payload, sig, client=None: sent.append((device.device_id, payload)) or True)
    store = Store(root / "plugin-data" / "herald-push")
    store.upsert(Device.parse(phone(ME)))
    store.upsert(Device.parse(phone(OTHER)))
    return root, store, sent


def pushed_to(sent, device_id):
    return [p["bot"] for d, p in sent if d == device_id and p["type"] == "bot_message"]


# ── the list itself ──────────────────────────────────────────────────────────

def test_a_phone_with_a_list_gets_exactly_those_profiles():
    allow = Allowlist({ME: ["default"]})
    assert allow.allows(ME, "default")
    assert not allow.allows(ME, "froebel")
    assert not allow.allows(ME, "a-profile-made-later")


def test_a_phone_with_no_list_is_sent_everything_as_before():
    allow = Allowlist({ME: ["default"]})
    assert allow.get(OTHER) is None
    assert allow.allows(OTHER, "froebel")


def test_lists_that_cant_be_read_fail_closed_to_default_only(tmp_path):
    store = Store(tmp_path)
    store.upsert(Device.parse(phone(ME)), ["default", "alice"])
    (tmp_path / "profiles.json").write_text("{not json", encoding="utf-8")
    allow = store.allowlist()
    assert allow.unreadable
    assert allow.allows(ME, "default") and allow.allows(OTHER, "default")
    assert not allow.allows(ME, "alice") and not allow.allows(OTHER, "froebel")
    (tmp_path / "profiles.json").write_text('{"%s": "froebel"}' % ME, encoding="utf-8")  # wrong shape too
    assert store.allowlist().unreadable


@pytest.mark.parametrize("bad", ["default", None, ["a b"], ["../x"], [""], [1], ["x" * 65], ["ok"] * 201, {"a": 1}])
def test_a_bad_list_is_refused_not_trimmed(tmp_path, bad):
    store = Store(tmp_path)
    store.upsert(Device.parse(phone(ME)))
    with pytest.raises(ValueError):
        store.set_allowed_profiles(ME, bad)
    assert store.allowed_profiles(ME) is None


def test_a_list_is_stored_sorted_and_without_repeats(tmp_path):
    store = Store(tmp_path)
    store.upsert(Device.parse(phone(ME)))
    assert store.set_allowed_profiles(ME, ["zed", "default", "zed"]) == ["default", "zed"]


def test_only_a_registered_phone_can_have_a_list(tmp_path):
    with pytest.raises(ValueError):
        Store(tmp_path).set_allowed_profiles(ME, ["default"])


def test_a_list_survives_a_restart(tmp_path):
    Store(tmp_path).upsert(Device.parse(phone(ME)), ["default"])
    reopened = Store(tmp_path)  # a new process, the same files
    assert reopened.allowed_profiles(ME) == ["default"]
    assert not reopened.allowlist().allows(ME, "froebel")


# ── registering, renewing and unregistering ─────────────────────────────────

def test_a_registration_carries_the_list_so_the_first_push_is_already_limited(tmp_path):
    store = Store(tmp_path)
    store.upsert(Device.parse(phone(ME)), ["default"])
    assert [d.device_id for d in store.devices()] == [ME]
    assert store.allowlist().allows(ME, "default") and not store.allowlist().allows(ME, "froebel")


def test_the_list_is_written_before_the_phone_is_listed(tmp_path, monkeypatch):
    """A reader that sees the phone must see its list: the file with the lists is written first."""
    store = Store(tmp_path)
    order = []
    from herald_push import store as store_module
    real = store_module._write_private
    monkeypatch.setattr(store_module, "_write_private", lambda path, data: (order.append(path.name), real(path, data))[1])
    store.upsert(Device.parse(phone(ME)), ["default"])
    assert order == ["profiles.json", "devices.json"]


def test_registering_again_without_a_list_leaves_the_list_alone(tmp_path):
    store = Store(tmp_path)
    store.upsert(Device.parse(phone(ME)), ["default"])
    store.upsert(Device.parse(phone(ME)))  # an older app, or a refresh that doesn't carry it
    assert store.allowed_profiles(ME) == ["default"]


def test_registering_again_with_a_list_replaces_it(tmp_path):
    store = Store(tmp_path)
    store.upsert(Device.parse(phone(ME)), ["default"])
    store.upsert(Device.parse(phone(ME)), ["default", "alice"])
    assert store.allowed_profiles(ME) == ["alice", "default"]


def test_a_renewed_phone_with_a_new_id_starts_with_the_list_it_registered_with(tmp_path):
    store = Store(tmp_path)
    store.upsert(Device.parse(phone(ME)), ["default"])
    new_id = "2b6f2c1e-1111-4222-8333-944445555666"
    store.upsert(Device.parse(phone(new_id)), ["default"])
    assert store.allowed_profiles(new_id) == ["default"]
    # The old identity is retired by the app; once the gateway forgets it, its list goes too.
    assert store.remove(ME)
    assert ME not in json.loads((tmp_path / "profiles.json").read_text())
    assert store.allowed_profiles(new_id) == ["default"]


def test_a_bad_list_in_a_registration_registers_nothing(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(InvalidRegistration):
        store.upsert(Device.parse(phone(ME)), ["../x"])
    assert store.devices() == [] and not (tmp_path / "profiles.json").exists()


def test_unregistering_forgets_the_list_and_a_returning_id_starts_with_none(tmp_path):
    store = Store(tmp_path)
    store.upsert(Device.parse(phone(ME)), ["default"])
    store.upsert(Device.parse(phone(OTHER)), ["default", "alice"])
    assert store.remove(ME)
    assert store.allowed_profiles(OTHER) == ["alice", "default"]
    store.upsert(Device.parse(phone(ME)))
    assert store.allowed_profiles(ME) is None


def test_clearing_goes_back_to_no_list(tmp_path):
    store = Store(tmp_path)
    store.upsert(Device.parse(phone(ME)), ["default"])
    store.clear_allowed_profiles(ME)
    assert store.allowed_profiles(ME) is None


# ── Bot Chat pushes ──────────────────────────────────────────────────────────

def test_a_limited_phone_hears_only_its_profiles_and_an_unlimited_one_hears_all(world):
    root, store, sent = world
    store.set_allowed_profiles(ME, ["default"])
    w = watcher.Watcher(root, store, now=lambda: NOW)
    w.poll()
    say(root, "from default")
    say(root / "profiles" / "froebel", "from froebel")
    w.poll()
    assert pushed_to(sent, ME) == ["default"]
    assert sorted(pushed_to(sent, OTHER)) == ["default", "froebel"]


def test_a_profile_made_after_the_list_was_set_stays_out(world):
    root, store, sent = world
    store.set_allowed_profiles(ME, ["default"])
    w = watcher.Watcher(root, store, now=lambda: NOW)
    w.poll()
    fresh = root / "profiles" / "made-later"
    make_db(fresh)
    w.poll()  # first sight of the new profile: its chat starts from now
    say(fresh, "hello from a profile nobody listed")
    w.poll()
    assert "made-later" not in pushed_to(sent, ME)
    assert "made-later" in pushed_to(sent, OTHER)


def test_allowing_a_profile_later_sends_what_comes_after_not_what_was_held_back(world):
    root, store, sent = world
    store.set_allowed_profiles(ME, ["default"])
    froebel = root / "profiles" / "froebel"
    w = watcher.Watcher(root, store, now=lambda: NOW)
    w.poll()
    say(froebel, "private one")
    w.poll()
    assert "froebel" not in pushed_to(sent, ME)
    store.set_allowed_profiles(ME, ["default", "froebel"])
    w.poll()  # the held-back reply is not sent now that it would be allowed
    assert "froebel" not in pushed_to(sent, ME)
    say(froebel, "next one")
    w.poll()
    assert pushed_to(sent, ME).count("froebel") == 1


def test_a_changed_list_applies_without_a_restart_and_a_restart_keeps_it(world):
    root, store, sent = world
    w = watcher.Watcher(root, store, now=lambda: NOW)
    w.poll()
    say(root / "profiles" / "froebel", "one")
    w.poll()
    assert pushed_to(sent, ME) == ["froebel"]  # no list yet: as before
    store.set_allowed_profiles(ME, ["default"])
    say(root / "profiles" / "froebel", "two")
    w.poll()
    assert pushed_to(sent, ME) == ["froebel"]  # the same watcher, the new list
    w2 = watcher.Watcher(root, Store(store.root), now=lambda: NOW)
    w2._marks = store.marks()
    say(root / "profiles" / "froebel", "three")
    w2.poll()
    assert pushed_to(sent, ME) == ["froebel"]  # after a restart too


def test_a_phone_with_an_empty_list_hears_no_bot_at_all(world):
    root, store, sent = world
    store.set_allowed_profiles(ME, [])
    w = watcher.Watcher(root, store, now=lambda: NOW)
    w.poll()
    say(root, "x")
    w.poll()
    assert pushed_to(sent, ME) == []


# ── cron-run pushes ──────────────────────────────────────────────────────────

def follow_everything(store, device_id):
    cron.Follows(store).set(device_id, cron.Follow({}, failures=True, default="notify"))


def cron_world(world):
    root, store, sent = world
    follow_everything(store, ME)
    follow_everything(store, OTHER)
    froebel = root / "profiles" / "froebel"
    w = watcher.Watcher(root, store)
    write_run(root, "base", "2026-10-11_08-00-00", agent_run())  # history is old news on the first look
    w.poll_cron(store.devices())
    return root, froebel, store, sent, w


def cron_to(sent, device_id):
    return sorted((p["bot"], p["status"]) for d, p in sent if d == device_id and p["type"] == "cron_run")


def test_a_followed_everything_phone_still_gets_no_run_of_a_profile_it_may_not_hear(world):
    root, froebel, store, sent, w = cron_world(world)
    store.set_allowed_profiles(ME, ["default"])
    write_run(root, "j", "2026-10-12_08-00-00", agent_run("j"))
    write_run(froebel, "j", "2026-10-12_08-00-00", agent_run("j", "private"))
    w.poll_cron(store.devices())
    assert cron_to(sent, ME) == [("default", "ok")]
    assert cron_to(sent, OTHER) == [("default", "ok"), ("froebel", "ok")]


def test_the_failures_switch_does_not_bring_back_an_excluded_profiles_failed_run(world):
    root, froebel, store, sent, w = cron_world(world)
    store.set_allowed_profiles(ME, ["default"])
    write_run(froebel, "j", "2026-10-12_09-00-00", FAILED_RUN)
    write_run(root, "k", "2026-10-12_09-00-00", FAILED_RUN)
    w.poll_cron(store.devices())
    assert cron_to(sent, ME) == [("default", "failed")]
    assert ("froebel", "failed") in cron_to(sent, OTHER)


def test_a_job_with_the_same_id_in_two_profiles_goes_only_where_allowed(world):
    root, froebel, store, sent, w = cron_world(world)
    store.set_allowed_profiles(ME, ["default"])
    cron.Follows(store).set(ME, cron.Follow({"shared": "notify"}))  # follows by job id alone
    write_run(froebel, "shared", "2026-10-12_10-00-00", agent_run("shared", "froebel's"))
    w.poll_cron(store.devices())
    assert cron_to(sent, ME) == []


def test_a_profile_made_later_has_no_runs_sent_to_a_limited_phone(world):
    root, froebel, store, sent, w = cron_world(world)
    store.set_allowed_profiles(ME, ["default"])
    write_run(root / "profiles" / "made-later", "j", "2026-10-12_10-00-00", agent_run("j"))
    w.poll_cron(store.devices())
    assert cron_to(sent, ME) == []


# ── the Inbox ────────────────────────────────────────────────────────────────

def inbox_world(tmp_path):
    root = tmp_path / "root"
    store = Store(root / "plugin-data" / "herald-push")
    store.upsert(Device.parse(phone(ME)))
    store.upsert(Device.parse(phone(OTHER)))
    write_run(root, "brief", "2026-10-12_08-00-00", agent_run("brief", "mine"))
    write_run(root / "profiles" / "froebel", "brief", "2026-10-12_08-00-00", agent_run("brief", "hers"))
    write_run(root / "profiles" / "froebel", "brief", "2026-10-12_09-00-00", FAILED_RUN)
    handle = commands.make_handler(store, "t", lambda: True, trusted=lambda: True, root=root)
    for device in (ME, OTHER):
        handle("cron follow " + json.dumps({"device_id": device, "default": "quiet", "failures": True}))
    return store, handle


def inbox(handle, device):
    return json.loads(handle("cron inbox " + json.dumps({"device_id": device})))["runs"]


def test_the_inbox_of_a_limited_phone_lists_only_its_profiles_failures_included(tmp_path):
    store, handle = inbox_world(tmp_path)
    assert len(inbox(handle, ME)) == 3  # no list yet: every profile's runs, as before
    assert len(inbox(handle, OTHER)) == 3
    store.set_allowed_profiles(ME, ["default"])
    assert [(r["bot"], r["text"]) for r in inbox(handle, ME)] == [("default", "mine")]
    assert len(inbox(handle, OTHER)) == 3  # another phone is unchanged


def test_reading_a_run_of_an_excluded_profile_is_refused_for_that_phone_only(tmp_path):
    store, handle = inbox_world(tmp_path)
    store.set_allowed_profiles(ME, ["default"])
    ask = {"bot": "froebel", "job": "brief", "run": "2026-10-12_08-00-00"}
    assert json.loads(handle("cron read " + json.dumps({"device_id": ME, **ask})))["ok"] is False
    assert json.loads(handle("cron read " + json.dumps({"device_id": OTHER, **ask})))["run"]["text"] == "hers"
    mine = {"bot": "default", "job": "brief", "run": "2026-10-12_08-00-00"}
    assert json.loads(handle("cron read " + json.dumps({"device_id": ME, **mine})))["run"]["text"] == "mine"


# ── the commands ─────────────────────────────────────────────────────────────

def handler(tmp_path, trusted=True, root=None):
    store = Store(tmp_path)
    return store, commands.make_handler(store, "t", lambda: True, trusted=lambda: trusted, root=root)


def test_register_with_profiles_stores_them_and_answers_with_them_and_the_features(tmp_path):
    store, handle = handler(tmp_path)
    answer = json.loads(handle("register " + json.dumps({**phone(ME), "profiles": ["default"]})))
    assert answer["ok"] and answer["profiles"] == ["default"] and "profile_allowlist" in answer["features"]
    assert store.allowed_profiles(ME) == ["default"]


def test_register_with_a_bad_list_is_refused_whole(tmp_path):
    store, handle = handler(tmp_path)
    answer = json.loads(handle("register " + json.dumps({**phone(ME), "profiles": "default"})))
    assert answer["ok"] is False and store.devices() == []


def test_register_without_profiles_keeps_an_existing_list(tmp_path):
    store, handle = handler(tmp_path)
    handle("register " + json.dumps({**phone(ME), "profiles": ["default"]}))
    answer = json.loads(handle("register " + json.dumps(phone(ME))))
    assert answer["ok"] and answer["profiles"] == ["default"]


def test_info_names_the_feature_so_the_app_need_not_guess_from_a_version(tmp_path):
    _, handle = handler(tmp_path)
    assert "profile_allowlist" in json.loads(handle("info"))["features"]


def test_profiles_get_set_clear(tmp_path):
    store, handle = handler(tmp_path)
    handle("register " + json.dumps(phone(ME)))
    me = {"device_id": ME}
    assert json.loads(handle("profiles get " + json.dumps(me))) == {"ok": True, "profiles": None}
    assert json.loads(handle("profiles set " + json.dumps({**me, "profiles": ["default"]})))["profiles"] == ["default"]
    assert json.loads(handle("profiles get " + json.dumps(me)))["profiles"] == ["default"]
    assert json.loads(handle("profiles set " + json.dumps({**me, "profiles": "default"})))["ok"] is False
    assert json.loads(handle("profiles clear " + json.dumps(me)))["profiles"] is None
    assert json.loads(handle("profiles get " + json.dumps({"device_id": OTHER})))["ok"] is False
    assert json.loads(handle("profiles nonsense " + json.dumps(me)))["ok"] is False
    assert json.loads(handle("profiles get not-json"))["ok"] is False


def test_profiles_commands_are_refused_off_the_dashboard(tmp_path):
    store, handle = handler(tmp_path, trusted=False)
    store.upsert(Device.parse(phone(ME)))
    answer = json.loads(handle("profiles set " + json.dumps({"device_id": ME, "profiles": ["froebel"]})))
    assert answer["ok"] is False and store.allowed_profiles(ME) is None


def test_the_synthetic_test_goes_through_the_same_check_as_real_pushes(tmp_path, monkeypatch):
    store, handle = handler(tmp_path)
    handle("register " + json.dumps({**phone(ME), "profiles": ["default"]}))
    sent = []
    monkeypatch.setattr(push, "publish", lambda device, payload, sig, client=None: sent.append(payload) or True)
    ask = lambda profile: json.loads(handle("profiles test " + json.dumps({"device_id": ME, "profile": profile})))  # noqa: E731
    assert ask("froebel") == {"ok": True, "allowed": False, "sent": False}
    assert sent == []
    assert ask("default") == {"ok": True, "allowed": True, "sent": True}
    assert sent[0]["type"] == "bot_message" and sent[0]["bot"] == "default" and "Synthetic" in sent[0]["text"]
    assert ask("../x")["ok"] is False


# ── this plugin is the one Herald asks for ───────────────────────────────────

def test_the_version_in_the_manifest_matches_the_one_the_plugin_reports():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    manifest = re.search(r"^version: (\S+)", (root / "plugin.yaml").read_text(encoding="utf-8"), re.M).group(1)
    code = re.search(r'^VERSION = "([^"]+)"', (root / "herald_push" / "plugin.py").read_text(encoding="utf-8"), re.M).group(1)
    assert manifest == code
    assert tuple(int(x) for x in manifest.split(".")) >= (0, 6, 0)
