"""Phone tools and profiles: an agent in a profile a phone doesn't take calls from can neither ring it nor read it.

Synthetic phones and profiles only.
"""

import json
import sys
import time
import types

import pytest

from herald_push import commands, crypto, phone, plugin
from herald_push.phone import Phones
from herald_push.store import Device, Store

MINE = "0b6f2c1e-1111-4222-8333-944445555666"
SPARE = "99999999-aaaa-bbbb-cccc-dddddddddddd"
KEY = "a" * 64
SPARE_KEY = "b" * 64


def push_phone(device_id):
    enc, sig = crypto.generate_key(), crypto.generate_key()
    return Device.parse({
        "device_id": device_id, "name": "Pixel", "server": "https://ntfy.sh", "push_topic": crypto.new_topic(),
        "reply_topic": crypto.new_topic(), "enc_pub": crypto.encode_public(enc.public_key()),
        "sig_pub": crypto.encode_public(sig.public_key()),
    })


def limited(tmp_path, tools=("phone_battery", phone.CALL_TOOL), profiles=("default",)):
    """A phone that takes calls from [profiles] only."""
    phones = Phones(Store(tmp_path))
    phones.report(MINE, KEY, "Pixel", list(tools), profiles=list(profiles))
    return phones


class FakeCtx:
    def __init__(self):
        self.tools = {}
        self.unloads = []

    def register_tool(self, name, toolset, schema, handler, check_fn=None, emoji=""):
        self.tools[name] = (handler, check_fn)

    def on_unload(self, callback):
        self.unloads.append(callback)


@pytest.fixture
def ctx():
    ctx = FakeCtx()
    yield ctx
    for stop in ctx.unloads:
        stop()



def eager_phone(phones, result):
    """A phone that answers whatever is asked of it as soon as it is asked: a call that wrongly gets through is
    answered, not left to time out, so it shows up in the result."""
    import threading
    stop = threading.Event()

    def run():
        while not stop.is_set():
            for request in phones.pending(MINE):
                phones.answer(MINE, request["id"], result, None)
            time.sleep(0.01)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return stop

# ── routing ──────────────────────────────────────────────────────────────────

def test_a_phone_with_a_list_takes_calls_only_from_its_profiles(tmp_path):
    phones = limited(tmp_path)
    assert phones.route("phone_battery", profile="default") == (MINE, None)
    device, refusal = phones.route("phone_battery", profile="froebel")
    assert device is None and ("doesn't" in refusal or "No phone takes calls" in refusal)
    # A profile made later, and one that can't be told, are not on the list either.
    assert phones.route("phone_battery", profile="made-later")[0] is None
    assert phones.route("phone_battery", profile="")[0] is None


def test_a_phone_with_no_list_takes_every_profile_as_before(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(MINE, KEY, "Pixel", ["phone_battery"])
    for profile in ("default", "froebel", "", None):
        assert phones.route("phone_battery", profile=profile)[0] == MINE


def test_the_phone_that_sent_the_message_decides_alone_and_isnt_replaced_by_another(tmp_path):
    phones = limited(tmp_path)
    phones.report(SPARE, SPARE_KEY, "Spare", ["phone_battery"])  # no list: takes everything
    phones.here(MINE, "sid-mine")
    # A turn that came from my phone, in a profile it doesn't take calls from: refused, not sent to the spare.
    device, refusal = phones.route("phone_battery", "sid-mine", "froebel")
    assert device is None and "doesn't take calls from this profile" in refusal
    assert phones.route("phone_battery", "sid-mine", "default")[0] == MINE


def test_a_turn_no_phone_sent_goes_only_to_a_phone_that_takes_its_profile(tmp_path):
    phones = limited(tmp_path)
    phones.report(SPARE, SPARE_KEY, "Spare", ["phone_battery"], profiles=["default", "froebel"])
    assert phones.route("phone_battery", profile="froebel")[0] == SPARE
    assert phones.route("phone_battery", profile="default")[0] in (MINE, SPARE)
    only_mine = limited(tmp_path / "x")
    assert only_mine.route("phone_battery", profile="froebel")[0] is None


def test_a_profile_that_no_phone_takes_isnt_offered_the_tool_at_all(tmp_path):
    phones = limited(tmp_path)
    assert phones.offered("phone_battery", "default")
    assert not phones.offered("phone_battery", "froebel")
    # Unknown at the time of asking: offered as before; the call itself is still checked.
    assert phones.offered("phone_battery", None)
    assert phones.offered("phone_battery")


# ── calls ────────────────────────────────────────────────────────────────────

def test_a_refused_call_writes_no_request_and_sends_no_push(tmp_path):
    woken = []
    store = Store(tmp_path)
    phones = Phones(store, wake=lambda d, r: woken.append(r))
    phones.report(MINE, KEY, "Pixel", [phone.CALL_TOOL], push_id=SPARE, profiles=["default"])
    answer = json.loads(phones.call(phone.CALL_TOOL, {"reason": "urgent"}, "s1", profile="froebel"))
    assert "error" in answer
    assert phones.pending(MINE) == [] and not list((tmp_path / "phone").glob("*.req.json"))
    time.sleep(0.1)
    assert woken == []


def test_an_allowed_call_names_its_profile_to_the_phone_and_in_the_push(tmp_path):
    woken = []
    phones = Phones(Store(tmp_path), wake=lambda d, r: woken.append(r))
    phones.report(MINE, KEY, "Pixel", [phone.CALL_TOOL], push_id=SPARE, profiles=["default", "alice"])

    def answer():
        deadline = time.time() + 5
        while time.time() < deadline:
            waiting = phones.pending(MINE)
            if waiting:
                assert waiting[0]["profile"] == "alice"
                phones.answer(MINE, waiting[0]["id"], {"status": "answered"}, None)
                return
            time.sleep(0.02)

    import threading
    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    phones.call(phone.CALL_TOOL, {"reason": "check-in"}, "s1", profile="alice")
    thread.join()
    deadline = time.time() + 2
    while not woken and time.time() < deadline:
        time.sleep(0.02)
    assert woken[0]["profile"] == "alice"
    message = plugin.wake_message(woken[0])
    assert message["type"] == "phone_call" and message["bot"] == "alice"
    assert plugin.wake_message({**woken[0], "tool": "phone_battery"})["bot"] == "alice"


def test_a_call_with_no_profile_in_the_request_pushes_no_bot_field(tmp_path):
    message = plugin.wake_message({"id": "r1", "tool": "phone_battery"})
    assert "bot" not in message


# ── the handler: which profile is calling ────────────────────────────────────

def test_the_handler_passes_the_calling_profile_and_a_froebel_call_is_refused(tmp_path, ctx, monkeypatch):
    monkeypatch.setattr(phone, "turn_profile", lambda *a, **k: "froebel")
    phones = limited(tmp_path)
    phone.register_tools(ctx, phones)
    handler, check = ctx.tools["phone_battery"]
    assert check() is False  # not offered to that agent at all
    stop = eager_phone(phones, {"level": 80})
    try:
        started = time.time()
        result = json.loads(handler({}))
    finally:
        stop.set()
    # Refused at once, with the reason, and the phone was never asked: it would have answered.
    assert "level" not in result and "profile" in result["error"]
    assert time.time() - started < 2


def test_a_call_me_from_froebel_does_not_ring_and_says_to_reach_the_user_another_way(tmp_path, ctx, monkeypatch):
    monkeypatch.setattr(phone, "turn_profile", lambda *a, **k: "froebel")
    phones = limited(tmp_path)
    phone.register_tools(ctx, phones)
    stop = eager_phone(phones, {"status": "answered"})
    try:
        started = time.time()
        result = json.loads(ctx.tools[phone.CALL_TOOL][0]({"reason": "Hello"}))
    finally:
        stop.set()
    # The phone would have answered; the call never reached it.
    assert result.get("status") != "answered"
    assert time.time() - started < 2


def test_the_default_profiles_agent_still_reaches_the_phone(tmp_path, ctx, monkeypatch):
    monkeypatch.setattr(phone, "turn_profile", lambda *a, **k: "default")
    phones = limited(tmp_path)
    phone.register_tools(ctx, phones)
    handler, check = ctx.tools["phone_battery"]
    assert check() is True

    import threading

    def answer():
        deadline = time.time() + 5
        while time.time() < deadline:
            waiting = phones.pending(MINE)
            if waiting:
                phones.answer(MINE, waiting[0]["id"], {"level": 80}, None)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    assert json.loads(handler({})) == {"level": 80}
    thread.join()


def fake_modules(monkeypatch, override=None, process="default", session=None, raises=False, multiplex=None, session_id=None):
    """Stand-ins for Hermes' own modules: [override] is the profile a turn bound as its home (None: nothing bound),
    [process] the profile the process home names (it falls back to the launch profile), [session] what the session
    says. The profile is read from them the way it is on a gateway."""
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home_override = lambda: ("/profiles/" + override) if override else None
    constants.get_hermes_home = lambda: "/profiles/" + (process or "")
    constants.profile_name_for_home = lambda path: str(path).split("/profiles/", 1)[-1] or None
    session_module = types.ModuleType("gateway.session_context")

    def get_session_env(name, default=""):
        if raises:
            raise RuntimeError("no context")
        return (session if name == "HERMES_SESSION_PROFILE" and session is not None else default)

    session_module.get_session_env = get_session_env
    if session_id is not None:
        inner = get_session_env
        session_module.get_session_env = lambda name, default="": session_id if name == "HERMES_SESSION_ID" else inner(name, default)
    if multiplex is not None:
        scope = types.ModuleType("agent.secret_scope")
        scope.is_multiplex_active = lambda: multiplex
        monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
        monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    else:
        monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_module)


def test_a_scheduled_jobs_profile_is_the_home_it_runs_under(monkeypatch):
    fake_modules(monkeypatch, override="froebel", session="", process="default")
    assert phone.turn_profile() == "froebel"


def test_a_dashboard_turns_profile_is_what_its_session_names(monkeypatch):
    fake_modules(monkeypatch, override=None, session="froebel", process="default")
    assert phone.turn_profile() == "froebel"
    fake_modules(monkeypatch, override=None, session="default", process="default")
    assert phone.turn_profile() == "default"
    fake_modules(monkeypatch, override="alice", session="alice", process="default")
    assert phone.turn_profile() == "alice"


def test_sources_that_disagree_are_not_guessed_at(monkeypatch):
    # A routed home against a session that says the launch profile: the one that could pass for the default loses.
    fake_modules(monkeypatch, override="froebel", session="default", process="default")
    assert phone.turn_profile() == ""
    fake_modules(monkeypatch, override="default", session="froebel", process="default")
    assert phone.turn_profile() == ""
    # Said to be the default while the process itself runs as another profile.
    fake_modules(monkeypatch, override=None, session="default", process="froebel")
    assert phone.turn_profile() == ""


def test_the_bare_process_home_is_never_trusted_on_its_own(monkeypatch):
    # Nothing bound: it would read every profile as the launch profile, "default".
    fake_modules(monkeypatch, override=None, session="", process="default")
    assert phone.turn_profile() == ""
    fake_modules(monkeypatch, override=None, session=None, process="froebel")
    assert phone.turn_profile() == ""


def test_whatever_cant_be_read_or_isnt_a_profile_name_is_unknown(monkeypatch):
    fake_modules(monkeypatch, override=None, session="x", process="default", raises=True)
    assert phone.turn_profile() == ""
    fake_modules(monkeypatch, override=None, session="bad name", process="default")
    assert phone.turn_profile() == ""
    fake_modules(monkeypatch, override="../evil", session="", process="default")
    assert phone.turn_profile() == ""
    monkeypatch.delitem(sys.modules, "hermes_constants")
    monkeypatch.delitem(sys.modules, "gateway.session_context")
    assert phone.turn_profile() == ""


def test_an_unknown_calling_profile_is_refused_by_a_phone_with_a_list_and_taken_by_one_without(tmp_path):
    phones = limited(tmp_path)
    assert phones.route("phone_battery", profile="")[0] is None
    legacy = Phones(Store(tmp_path / "legacy"))
    legacy.report(MINE, KEY, "Pixel", ["phone_battery"])
    assert legacy.route("phone_battery", profile="")[0] == MINE


def make_profiles(tmp_path, with_session=None):
    """A root with the default profile and froebel, each with a state.db whose sessions table holds [with_session]:
    a dict of profile name to the session ids it stores."""
    import sqlite3
    homes = {"default": tmp_path, "froebel": tmp_path / "profiles" / "froebel", "alice": tmp_path / "profiles" / "alice"}
    for name, home in homes.items():
        home.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(home / "state.db")
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
        for sid in (with_session or {}).get(name, []):
            conn.execute("INSERT INTO sessions (id) VALUES (?)", (sid,))
        conn.commit()
        conn.close()
    return tmp_path


def test_an_ordinary_default_job_in_a_process_that_serves_one_profile_is_the_default(monkeypatch):
    # No override, no session profile (a cron job's is empty): the process is the profile.
    fake_modules(monkeypatch, override=None, session="", process="default", multiplex=False)
    assert phone.turn_profile() == "default"
    fake_modules(monkeypatch, override=None, session="", process="froebel", multiplex=False)
    assert phone.turn_profile() == "froebel"


def test_nothing_bound_in_a_process_serving_several_profiles_is_found_by_its_session(tmp_path, monkeypatch):
    root = make_profiles(tmp_path, {"froebel": ["s-froebel-1"], "default": ["s-mine-1"]})
    fake_modules(monkeypatch, override=None, session="", process="default", multiplex=True)
    assert phone.turn_profile("s-froebel-1", root) == "froebel"
    assert phone.turn_profile("s-mine-1", root) == "default"
    # Its own session id from the context, when the handler is given none.
    fake_modules(monkeypatch, override=None, session="", process="default", multiplex=True, session_id="s-froebel-1")
    assert phone.turn_profile("", root) == "froebel"


def test_a_session_that_is_nowhere_or_in_two_profiles_or_not_an_id_is_unknown(tmp_path, monkeypatch):
    root = make_profiles(tmp_path, {"froebel": ["dup", "only-froebel"], "alice": ["dup"]})
    fake_modules(monkeypatch, override=None, session="", process="default", multiplex=True)
    assert phone.turn_profile("nowhere", root) == ""
    assert phone.turn_profile("dup", root) == ""
    assert phone.turn_profile("../evil", root) == ""
    assert phone.turn_profile("", root) == ""
    assert phone.turn_profile("only-froebel", None) == ""
    assert phone.turn_profile("only-froebel", root) == "froebel"


def test_when_it_cant_be_told_whether_the_process_serves_several_profiles_it_does_not_trust_the_home(tmp_path, monkeypatch):
    root = make_profiles(tmp_path, {"froebel": ["s-1"]})
    fake_modules(monkeypatch, override=None, session="", process="default", multiplex=None)  # no such module
    assert phone.turn_profile("s-1", root) == "froebel"
    assert phone.turn_profile("unknown", root) == ""


def test_disagreeing_sources_are_not_rescued_by_the_fallbacks(tmp_path, monkeypatch):
    root = make_profiles(tmp_path, {"default": ["s-1"]})
    fake_modules(monkeypatch, override="froebel", session="default", process="default", multiplex=False)
    assert phone.turn_profile("s-1", root) == ""


def test_the_handler_finds_a_default_jobs_profile_and_it_reaches_the_phone(tmp_path, ctx, monkeypatch):
    """A default-profile cron job's call_me: nothing bound, one profile in the process, so it is the default."""
    import threading
    fake_modules(monkeypatch, override=None, session="", process="default", multiplex=False)
    phones = limited(tmp_path)
    phone.register_tools(ctx, phones)
    stop = eager_phone(phones, {"status": "answered"})
    try:
        result = json.loads(ctx.tools[phone.CALL_TOOL][0]({"reason": "Seven thirty"}))
    finally:
        stop.set()
    assert result.get("status") == "answered"


def test_in_a_process_serving_several_profiles_the_handler_tells_them_apart_by_the_turns_session(tmp_path, ctx, monkeypatch):
    root = make_profiles(tmp_path / "hermes", {"froebel": ["s-froebel"], "default": ["s-mine"]})
    fake_modules(monkeypatch, override=None, session="", process="default", multiplex=True)
    phones = Phones(Store(root / "plugin-data" / "herald-push"))
    phones.report(MINE, KEY, "Pixel", ["phone_battery", phone.CALL_TOOL], profiles=["default"])
    phone.register_tools(ctx, phones)
    handler = ctx.tools["phone_battery"][0]
    call_me = ctx.tools[phone.CALL_TOOL][0]
    stop = eager_phone(phones, {"level": 80, "status": "answered"})
    try:
        # The same for a ring.
        rings_hers = json.loads(call_me({"reason": "Hello"}, session_id="s-froebel"))
        rings_mine = json.loads(call_me({"reason": "Hello"}, session_id="s-mine"))
        # Her agent's call: the session is hers, so the phone is never asked.
        hers = json.loads(handler({}, session_id="s-froebel"))
        # Yours: the session is in your profile, so it reaches the phone.
        mine = json.loads(handler({}, session_id="s-mine"))
        # A session no profile holds can't be attributed, and is refused too.
        nobody = json.loads(handler({}, session_id="s-unknown"))
    finally:
        stop.set()
    assert rings_hers.get("status") != "answered"
    assert rings_mine.get("status") == "answered"
    assert "level" not in hers and "error" in hers
    assert mine["level"] == 80
    assert "level" not in nobody and "error" in nobody


# ── the list for a phone that only uses phone tools ──────────────────────────

def test_a_phone_with_only_phone_tools_has_its_list_without_a_push_registration(tmp_path):
    store = Store(tmp_path)
    phones = Phones(store)
    phones.report(MINE, KEY, "Pixel", ["phone_battery"], profiles=["default"])
    assert store.devices() == []
    assert store.allowed_profiles(MINE) == ["default"]
    assert phones.route("phone_battery", profile="froebel")[0] is None


def test_the_list_is_stored_before_the_phone_is_listed(tmp_path, monkeypatch):
    from herald_push import store as store_module
    written = []
    real = store_module._write_private
    spy = lambda path, data: (written.append(path.name), real(path, data))[1]  # noqa: E731
    # Each module holds its own reference to the writer.
    monkeypatch.setattr(store_module, "_write_private", spy)
    monkeypatch.setattr(phone, "_write_private", spy)
    Phones(Store(tmp_path)).report(MINE, KEY, "Pixel", ["phone_battery"], profiles=["default"])
    assert written.index("profiles.json") < written.index("phones.json")


def test_a_bad_list_in_a_phone_report_changes_nothing(tmp_path):
    phones = Phones(Store(tmp_path))
    with pytest.raises(ValueError):
        phones.report(MINE, KEY, "Pixel", ["phone_battery"], profiles=["../etc"])
    assert phones._phones() == {} and phones.store.allowed_profiles(MINE) is None


def test_a_report_without_a_list_leaves_the_phones_list_alone(tmp_path):
    phones = limited(tmp_path)
    phones.report(MINE, KEY, "Pixel", ["phone_battery"])  # an older app
    assert phones.store.allowed_profiles(MINE) == ["default"]


def test_unregistering_push_keeps_the_list_of_a_phone_still_using_phone_tools(tmp_path):
    store = Store(tmp_path)
    store.upsert(push_phone(MINE), ["default"])
    Phones(store).report(MINE, KEY, "Pixel", ["phone_battery"])
    assert store.remove(MINE)
    assert store.allowed_profiles(MINE) == ["default"]
    # Once phone tools forget the phone too, nothing knows it any more and its list goes.
    Phones(store).remove(MINE)
    assert store.allowed_profiles(MINE) is None


def test_the_commands_set_and_read_a_phone_tool_phones_list(tmp_path):
    store = Store(tmp_path)
    phones = Phones(store)
    handle = commands.make_handler(store, "t", lambda: True, trusted=lambda: True, phones=phones)
    body = {"device_id": MINE, "key": KEY, "name": "Pixel", "tools": ["phone_battery"], "profiles": ["default"]}
    assert json.loads(handle("phone tools " + json.dumps(body)))["ok"] is True
    assert json.loads(handle("profiles get " + json.dumps({"device_id": MINE})))["profiles"] == ["default"]
    ask = {"device_id": MINE, "key": KEY, "profiles": ["default", "alice"]}
    assert json.loads(handle("profiles set " + json.dumps(ask)))["profiles"] == ["alice", "default"]
    # Not a push phone: a synthetic push has nowhere to go.
    assert json.loads(handle("profiles test " + json.dumps({"device_id": MINE, "profile": "default"})))["ok"] is False
    bad = json.loads(handle("phone tools " + json.dumps({**body, "profiles": "default"})))
    assert bad["ok"] is False


def test_only_the_phones_own_key_can_change_who_may_call_it(tmp_path):
    store = Store(tmp_path)
    phones = Phones(store)
    handle = commands.make_handler(store, "t", lambda: True, trusted=lambda: True, phones=phones)
    handle("phone tools " + json.dumps({"device_id": MINE, "key": KEY, "name": "Pixel", "tools": ["phone_battery"], "profiles": ["default"]}))
    widen = {"device_id": MINE, "profiles": ["default", "froebel"]}
    # Any signed-in dashboard session can name the phone's id; without its key, nothing changes.
    for extra in ({}, {"key": SPARE_KEY}, {"key": 5}):
        assert json.loads(handle("profiles set " + json.dumps({**widen, **extra})))["error"] == "bad key"
        assert json.loads(handle("profiles clear " + json.dumps({"device_id": MINE, **extra})))["error"] == "bad key"
    assert store.allowed_profiles(MINE) == ["default"]
    assert phones.route("phone_battery", profile="froebel")[0] is None
    assert json.loads(handle("profiles set " + json.dumps({**widen, "key": KEY})))["ok"] is True
    assert phones.route("phone_battery", profile="froebel")[0] == MINE
    # Reading it needs no key: it names no secret.
    assert json.loads(handle("profiles get " + json.dumps({"device_id": MINE})))["profiles"] == ["default", "froebel"]


def test_a_push_phones_list_is_still_set_by_the_app_without_a_key(tmp_path):
    store = Store(tmp_path)
    store.upsert(push_phone(SPARE))
    handle = commands.make_handler(store, "t", lambda: True, trusted=lambda: True, phones=Phones(store))
    assert json.loads(handle("profiles set " + json.dumps({"device_id": SPARE, "profiles": ["default"]})))["ok"] is True


def test_the_version_in_the_manifest_still_matches(tmp_path):
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert re.search(r"^version: (\S+)", (root / "plugin.yaml").read_text(encoding="utf-8"), re.M).group(1) == plugin.VERSION


def test_a_phone_with_no_list_of_its_own_uses_the_one_of_the_push_registration_it_is_linked_to(tmp_path):
    store = Store(tmp_path)
    store.upsert(push_phone(SPARE), ["default"])
    phones = Phones(store)
    # Reported by an app that sent no list, but linked to the push registration that has one.
    phones.report(MINE, KEY, "Pixel", ["phone_battery"], push_id=SPARE)
    assert phones.route("phone_battery", profile="froebel")[0] is None
    assert phones.route("phone_battery", profile="default")[0] == MINE
    assert not phones.offered("phone_battery", "froebel") and phones.offered("phone_battery", "default")
    # Its own list wins over the linked one.
    phones.report(MINE, KEY, "Pixel", ["phone_battery"], push_id=SPARE, profiles=["default", "froebel"])
    assert phones.route("phone_battery", profile="froebel")[0] == MINE


def test_a_phone_linked_to_a_push_registration_without_a_list_takes_every_profile(tmp_path):
    store = Store(tmp_path)
    store.upsert(push_phone(SPARE))
    phones = Phones(store)
    phones.report(MINE, KEY, "Pixel", ["phone_battery"], push_id=SPARE)
    assert phones.route("phone_battery", profile="froebel")[0] == MINE
