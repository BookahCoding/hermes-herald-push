# herald-push

A Hermes plugin that sends your bots' messages to the [Herald](https://github.com/Giton22/herald) Android app
even when the phone can't reach the gateway (off the tailnet, on mobile data). Messages travel through
[ntfy](https://ntfy.sh), are **end-to-end encrypted** to the phone, and are **signed** by the gateway. ntfy
only ever sees opaque envelopes on unguessable topics. Google services are not involved.

## Install

From Herald: **Settings → Notifications anywhere**. Herald installs this plugin on the gateway it's signed in
to and registers the phone, in one step.

Or by hand on the gateway:

```
hermes plugins install https://github.com/Giton22/hermes-herald-push
```

## How it works

- **Registration.** The phone registers over the dashboard's authenticated connection with
  `/herald-push register`. The command refuses to run on messaging platforms, so someone chatting with a bot on
  Telegram can't add their own phone. At registration the phone and the gateway swap public keys, and each pins
  the other's.
- **Watching.** One watcher, in the dashboard or the messaging gateway process, reads every profile's
  `state.db` read-only and pushes each new final reply in a bot's Bot Chat.
- **Crypto.** P-256 ECDH to an ephemeral key, HKDF-SHA256, AES-256-GCM bound to the topic, and ECDSA P-256
  over everything, verified before decrypting. The wire format is in Herald's
  [`docs/push-protocol.md`](https://github.com/Giton22/herald/blob/master/docs/push-protocol.md).
- **Storage.** The gateway identity and the device list are in `<hermes root>/plugin-data/herald-push/`, with
  file mode 0600.

**Not yet:** replying or approving through push. Those still use the direct connection.

## Which profiles a phone hears from (0.6.0)

Every phone can have a list of the profiles it may be sent. A phone **with a list** gets exactly those: a profile
that isn't on it, whether the plugin has never seen it or it was made after the list was, is not sent. A phone
**without a list** is sent every profile, as before.

- **Where it applies.** Before a Bot Chat reply is pushed; before a cron run is pushed, ahead of the `failures`
  and `default` follow rules, so neither brings back a profile the phone isn't allowed; and in `cron inbox` and
  `cron read`. Phone-tool requests and calls are not filtered by profile.
- **Registering.** The list travels with the registration (`profiles` in the `register` JSON) and is stored first,
  so a renewed phone is limited from its first push. A registration without `profiles` leaves an existing list as
  it is. `unregister` forgets the list. The lists are in `profiles.json` (0600), read on every poll, so a change
  applies at once and a restart keeps it.
- **Commands.** `/herald-push profiles get|set|clear|test <json>` with the phone's `device_id`: `set` takes
  `profiles` (a list of names), `test` takes a `profile` and sends one synthetic message through the same check as
  a real push. `info` and `register` answer with `features: ["profile_allowlist"]`, which Herald checks for.
- **If the lists can't be read** the plugin falls back to `default` only for every phone, never to everything.
- **Phone tools follow the same list.** A call is routed only to a phone that takes calls from the profile whose
  agent makes it, so a profile that isn't on a phone's list can neither ring that phone (`phone_call_me`) nor read
  it, and is not even offered the tools when no phone takes its calls. The phone that sent the message still
  decides alone: it is never replaced by another one. A phone's list is the one it reported with `phone tools`
  (`profiles` in the JSON), else the list of the push registration it is linked to, else none (every profile). A
  profile that can't be told counts as not on a list. The request and the push for it name the profile (`bot`), so
  the phone can check it too.
- **Which profile is calling.** Hermes can run several profiles' agents in one process, and the process home falls
  back to the launch profile, which reads as `default`. So the profile is taken, in order, from what the turn binds
  for itself (the profile home a scheduled job or a routed turn runs under, and the profile its session names; two
  that disagree, or a `default` the process contradicts, are unknown); else, in a process that serves only one
  profile, that profile (an ordinary default-profile cron job); else, in a process serving several, the one profile
  whose `state.db` holds the turn's session. Anything else is unknown, which a phone with a list refuses. Each
  profile loads its own copy of this plugin, so a profile whose copy is older than 0.6.0 has phone tools that
  don't check any of this: update that copy too.
- **Who may change a phone's list.** A phone that uses phone tools has a secret key, and `profiles set` and
  `profiles clear` for it need that key; reading a list does not.
- **Every copy must be 0.6.0.** A profile with its own older copy of this plugin does not know about the lists.

## Cron runs (0.4.0)

Finished cron runs for Herald's Inbox, for the jobs a phone follows.

- **Read from the output files.** Hermes writes every finished run to `<home>/cron/output/<job>/<time>.md`,
  whatever the job delivers to. The watcher reads those, in every profile, so a job keeps going to Telegram or
  wherever it went before; following it here only adds the phone.
- **Per job, per phone.** Herald sends `/herald-push cron follow` with `device_id`, `jobs` (job id to `notify`,
  `quiet` or `off`), `default` (what every job not listed does, `off` unless asked; with `quiet` or `notify` a
  job made later follows too) and `failures` (any failed run notifies, followed or not). `cron following` reads it back. The
  list is in `cron.json` (0600) and is kept per registered phone.
- **What is sent.** A `cron_run` push: `bot` (profile), `job`, `run` (the output file's name), `label` (the
  job's name), `status` (`ok` or `failed`), `notify`, `at` and the start of the output as `text` (about 1,800
  characters, like a bot message). Herald fetches the whole run over the direct connection.
- **The Inbox.** `cron inbox` lists the runs a phone follows (and every failure, when it asked), newest first,
  each with the start of its text; `limit` and `before` page back. `cron read` gives one run in full (`bot`,
  `job`, `run`, up to 48 KB). Both read the same output files, so they work for every profile.
- **What isn't.** Runs that stayed silent (`[SILENT]`, a script with no output, a gate that didn't wake the
  agent), a job's history when it is first seen, and runs older than 30 minutes when found (after a restart).

## Phone tools (0.3.3)

Agent tools that run on the phone, in the `herald_phone` toolset: `phone_battery`, `phone_network`,
`phone_sound`, `phone_alarm`, `phone_screen_time`, `phone_open`, `phone_clipboard`, `phone_location`,
`phone_geofence`, `phone_calendar`, `phone_contacts`, `phone_health` and `phone_call_me` (0.5.0).

- **Off until the user turns them on.** Each tool has its own switch in Herald (Settings → Phone tools). Herald
  tells the gateway which ones are on with `/herald-push phone tools`, and a tool no phone has on is not
  offered to the agent at all. Hermes fixes a chat's tools when the chat starts, so a switch changes what new
  chats get; a call from an older chat to a tool that's off now is refused, and so is a call still waiting.
- **Not on messaging platforms.** A turn from Telegram, Discord and the like gets a refusal: others may be
  talking to the bot there.
- **One phone, one key.** The first report binds the phone's id to a secret key (the gateway keeps its
  hash), and every later phone command must carry it.
- **The phone you chat from.** Herald tells the gateway which chat connections it sends messages on
  (`/herald-push phone here`), and a call from a turn started there goes to that phone, even when another
  phone has the same tool on. If that phone has the tool off, the call is refused rather than sent to another
  phone. A turn without a chat connection (a side question or background task, which Hermes runs apart from
  the chat) goes to a phone that sent a turn in the last two minutes. Any other turn no phone started (a cron
  job, Desktop, the dashboard) goes to the first phone, in the order the user set in Herald, that has the tool
  on; phones not placed follow by last use. Herald lists the phones (`/herald-push phone list`), orders them
  (`phone order`) and removes one the user doesn't have any more (`phone remove`); the list shows names and
  switches, never another phone's key or push registration.
- **Changes ask first.** Setting an alarm or Do Not Disturb, opening something, copying, adding or moving an
  event and adding a watched place are shown on the phone, and only happen if the user allows them there.
- **How a call travels.** The tool writes a request file to `<hermes root>/plugin-data/herald-push/phone/`
  (files, because the agent can run in another process than the dashboard). Herald learns of it from the
  chat's tool event, a push, or when it connects, reads it with `/herald-push phone pending` and answers with
  `/herald-push phone answer`, both over the dashboard's authenticated connection only. Reads wait 30 seconds
  for the phone, changes 2 minutes; then the agent is told the phone didn't answer.
- **The agent calls you.** `phone_call_me(reason, details?)` rings the user's own phone like an incoming call
  (and Herald on their watch). It takes no number: it can only ring a phone that switched it on in Herald
  (Settings → Phone tools → Calls). Answering puts Hermes on the line by voice, opening with the reason. The
  tool waits up to 75 seconds (the phone rings 45) and returns `answered`, `declined`, `no_answer`, `busy`
  (already on a call with Hermes), `quiet` (quiet hours on the phone, 0.5.1) or `unreachable`, each with a note; anything but `answered` tells the agent
  to reach the user another way. The push carries the reason and details (end-to-end encrypted, like every
  push), so the phone rings even when Herald isn't connected; the answer comes back over the socket. It works
  from scheduled jobs too, for "call me at 7:30".

## Tests

```
pip install cryptography httpx pyyaml pytest
pytest
```
