import json
import os
import time

import pytest

from herald_push import commands, crypto, cron, push, watcher
from herald_push.store import Device, Store


def phone(device_id="0b6f2c1e-1111-4222-8333-944445555666"):
    enc, sig = crypto.generate_key(), crypto.generate_key()
    data = {
        "device_id": device_id, "name": "Pixel", "server": "https://ntfy.sh",
        "push_topic": crypto.new_topic(), "reply_topic": crypto.new_topic(),
        "enc_pub": crypto.encode_public(enc.public_key()), "sig_pub": crypto.encode_public(sig.public_key()),
    }
    return data, enc


def agent_run(name="morning-briefing", job="006998ffc458", response="# Morning\n\nAll good."):
    return (f"# Cron Job: {name}\n\n**Job ID:** {job}\n**Run Time:** 2026-10-12 08:02:06\n**Schedule:** 0 8 * * *\n\n"
            f"## Prompt\n\nDo the briefing.\n\n## Script Output\n\n## Response\n\n{response}\n")


def write_run(home, job, stem, doc, age=60):
    folder = home / "cron" / "output" / job
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.md"
    path.write_text(doc, encoding="utf-8")
    then = time.time() - age
    os.utime(path, (then, then))
    return path


# ── reading a run ────────────────────────────────────────────────────────────

def test_an_agent_runs_reply_is_its_text():
    assert cron.parse_run(agent_run()) == ("morning-briefing", cron.OK, "# Morning\n\nAll good.")


def test_a_reply_that_stayed_silent_is_not_a_run():
    assert cron.parse_run(agent_run(response="[SILENT]")) is None
    assert cron.parse_run(agent_run(response="")) is None


def test_a_failed_run_says_why_and_drops_the_failed_tag_from_its_name():
    doc = ("# Cron Job: Email daily digest (FAILED)\n\n**Job ID:** 53dce36a473d\n**Run Time:** 2026-09-16 21:06:33\n\n"
           "## Prompt\n\nDigest.\n\n## Error\n\n```\nRuntimeError: no reply\n```\n")
    assert cron.parse_run(doc) == ("Email daily digest", cron.FAILED, "RuntimeError: no reply")


def test_a_scripts_output_is_its_text_and_a_silent_script_is_nothing():
    header = "# Cron Job: tool-health-check\n\n**Job ID:** f9ec66b227a6\n**Run Time:** 2026-10-05 08:00:54\n**Mode:** no_agent (script)\n"
    assert cron.parse_run(header + "\n---\n\n2 issues found\n\n---\n") == ("tool-health-check", cron.OK, "2 issues found")
    assert cron.parse_run(header + "**Status:** silent (empty output)\n") is None
    assert cron.parse_run(header + "**Status:** silent (wakeAgent=false)\n") is None
    assert cron.parse_run(header + "**Status:** script failed\n\nexit 2\n")[1:] == (cron.FAILED, "exit 2")


def test_a_gated_run_that_never_woke_the_agent_is_nothing():
    doc = "# Cron Job: Email trainer\n\n**Job ID:** b1365b96ae90\n**Run Time:** 2026-10-02 11:00:51\n\nScript gate returned `wakeAgent=false` — agent skipped."
    assert cron.parse_run(doc) is None


def test_the_schedulers_last_ditch_error_is_a_failure():
    assert cron.parse_run("# Cron Job: x\n\nError: boom\n") == ("x", cron.FAILED, "boom")


# ── what a phone follows ─────────────────────────────────────────────────────

def test_follow_decides_what_is_sent_and_whether_it_rings():
    follow = cron.Follow({"a": "notify", "b": "quiet"}, failures=True)
    assert follow.wants("a", cron.OK) is True
    assert follow.wants("b", cron.OK) is False
    assert follow.wants("c", cron.OK) is None
    # Any failure rings when the phone asked for failures, even a job it doesn't follow or keeps quiet.
    assert follow.wants("b", cron.FAILED) is True
    assert follow.wants("c", cron.FAILED) is True
    assert cron.Follow({"c": "quiet"}).wants("c", cron.FAILED) is False


@pytest.mark.parametrize("bad", [
    {"jobs": {"../x": "notify"}}, {"jobs": {"a": "loud"}}, {"jobs": []}, {"failures": "yes"},
    {"jobs": {f"j{i}": "quiet" for i in range(cron.MAX_JOBS + 1)}},
])
def test_follow_is_validated(bad):
    with pytest.raises(ValueError):
        cron.Follow.parse(bad)


def test_only_a_registered_phone_can_follow(tmp_path):
    store = Store(tmp_path)
    follows = cron.Follows(store)
    with pytest.raises(ValueError):
        follows.set("0b6f2c1e-1111-4222-8333-944445555666", cron.Follow({"a": "notify"}))
    data, _ = phone()
    store.upsert(Device.parse(data))
    follows.set(data["device_id"], cron.Follow({"a": "notify"}, failures=True))
    assert follows.get(data["device_id"]) == cron.Follow({"a": "notify"}, True)
    assert oct(os.stat(follows.path).st_mode & 0o777) == "0o600"


def test_cron_command_follows_and_reports(tmp_path):
    store = Store(tmp_path)
    data, _ = phone()
    store.upsert(Device.parse(data))
    handle = commands.make_handler(store, "t", lambda: True, trusted=lambda: True)
    body = json.dumps({"device_id": data["device_id"], "jobs": {"006998ffc458": "notify"}, "failures": True})
    assert json.loads(handle("cron follow " + body)) == {"ok": True, "jobs": {"006998ffc458": "notify"}, "failures": True}
    asked = json.loads(handle("cron following " + json.dumps({"device_id": data["device_id"]})))
    assert asked["jobs"] == {"006998ffc458": "notify"}
    assert json.loads(handle("cron follow {\"device_id\": \"nope\"}"))["ok"] is False
    assert json.loads(handle("cron follow not-json"))["ok"] is False


def test_cron_command_is_refused_off_the_dashboard(tmp_path):
    handle = commands.make_handler(Store(tmp_path), "t", lambda: True, trusted=lambda: False)
    assert json.loads(handle("cron following {}"))["ok"] is False


# ── finding new runs ─────────────────────────────────────────────────────────

def test_a_jobs_history_is_not_news_but_its_next_run_is(tmp_path):
    home = tmp_path / "home"
    write_run(home, "j1", "2026-10-11_08-02-07", agent_run(response="old"))
    marks = {}
    found, changed = cron.CronWatcher().poll([("default", home)], marks)
    assert found == [] and changed and marks == {"cron:default:j1": "2026-10-11_08-02-07"}

    write_run(home, "j1", "2026-10-12_08-02-07", agent_run(response="new"))
    found, _ = cron.CronWatcher().poll([("default", home)], marks)
    assert [r.text for r in found] == ["new"]
    assert found[0].job_id == "j1" and found[0].run == "2026-10-12_08-02-07" and found[0].name == "morning-briefing"
    assert cron.CronWatcher().poll([("default", home)], marks)[0] == []


def test_a_run_still_being_written_waits_and_a_stale_one_is_skipped(tmp_path):
    home = tmp_path / "home"
    write_run(home, "j1", "2026-10-10_08-00-00", agent_run())
    marks = {}
    cron.CronWatcher().poll([("default", home)], marks)
    write_run(home, "j1", "2026-10-11_08-00-00", agent_run(response="stale"), age=cron.STALE_SECONDS + 60)
    write_run(home, "j1", "2026-10-12_08-00-00", agent_run(response="fresh"), age=0)
    found, _ = cron.CronWatcher().poll([("default", home)], marks)
    assert found == [] and marks["cron:default:j1"] == "2026-10-11_08-00-00"
    found, _ = cron.CronWatcher(now=lambda: time.time() + 5).poll([("default", home)], marks)
    assert [r.text for r in found] == ["fresh"]


def test_runs_are_found_in_every_profile_and_odd_names_are_ignored(tmp_path):
    root = tmp_path / "root"
    write_run(root, "j1", "2026-10-11_08-00-00", agent_run())
    write_run(root / "profiles" / "bot", "j2", "2026-10-11_08-00-00", agent_run())
    (root / "cron" / "output" / "j1" / "notes.md").write_text("x")
    marks = {}
    cron.CronWatcher().poll(watcher.profile_homes(root), marks)
    assert set(marks) == {"cron:default:j1", "cron:bot:j2"}


# ── end to end ───────────────────────────────────────────────────────────────

def test_the_watcher_pushes_a_followed_run_that_opens_on_the_phone(tmp_path, monkeypatch):
    root = tmp_path / "root"
    store = Store(root / "plugin-data" / "herald-push")
    data, enc = phone()
    other, _ = phone("1b6f2c1e-1111-4222-8333-944445555666")
    store.upsert(Device.parse(data))
    store.upsert(Device.parse(other))
    cron.Follows(store).set(data["device_id"], cron.Follow({"j1": "notify"}))

    sent = []
    monkeypatch.setattr(push, "publish", lambda device, payload, sig, client=None: sent.append((device, payload)) or True)
    w = watcher.Watcher(root, store)
    write_run(root, "j1", "2026-10-11_08-00-00", agent_run())
    assert w.poll_cron(store.devices()) == 0
    write_run(root, "j1", "2026-10-12_08-00-00", agent_run(response="Rain later. " + "x" * 3000))
    assert w.poll_cron(store.devices()) == 1

    device, payload = sent[0]
    assert device.device_id == data["device_id"]
    assert payload["type"] == "cron_run" and payload["job"] == "j1" and payload["run"] == "2026-10-12_08-00-00"
    assert payload["label"] == "morning-briefing" and payload["status"] == "ok" and payload["notify"] is True
    assert payload["text"].startswith("Rain later.") and payload["text"].endswith("…")

    # Marks survive a restart: a new watcher doesn't send the same run again.
    assert watcher.Watcher(root, store).poll_cron(store.devices()) == 0

    _, _, sig = store.identity()
    body = push.envelope_for(device, payload, sig)
    assert len(body) <= push.MAX_BODY
    opened = crypto.open_envelope(crypto.Envelope.from_json(body), device.push_topic, crypto.G2P, enc,
                                  crypto.decode_public(store.public_identity()["sig_pub"]))
    assert json.loads(opened)["text"] == payload["text"]
