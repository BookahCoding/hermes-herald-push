"""Cron runs for Herald's Inbox: which jobs each phone follows, and the finished runs to tell it about.

Hermes writes every finished run of a job to ``<home>/cron/output/<job_id>/<YYYY-MM-DD_HH-MM-SS>.md``, whatever
the job delivers to (Telegram, local, a Bot Chat). The watcher reads those files, never the jobs' own delivery,
so following a job here changes nothing about where else its output goes.

A phone says which jobs it follows with ``/herald-push cron follow <json>``: per job ``notify`` (a notification)
or ``quiet`` (into the Inbox only), plus ``failures`` for a notification when any job's run fails. Runs that
stayed silent (``[SILENT]``, a script with no output) are never sent.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .store import DEVICE_ID_RE, Store, _write_private

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
RUN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MODES = ("notify", "quiet")
# A job can be switched off on its own, when the phone follows every job by default.
JOB_MODES = MODES + ("off",)
MAX_JOBS = 200
# A run older than this when first seen (a restart, a phone that just followed the job) is old news.
STALE_SECONDS = 30 * 60
# Output files are written once, at the end of a run; one this fresh may still be being written.
SETTLE_SECONDS = 2.0
MAX_READ = 256 * 1024
MAX_LABEL = 64
# The Inbox list carries the start of each run; a run read in full is capped at this.
LIST_TEXT = 600
MAX_TEXT = 48 * 1024
MAX_LIST = 100
# At most this many files are read for one page of the Inbox: 40 jobs' worth at the scheduler's default
# retention, so it only binds where runs are kept forever.
MAX_SCAN = 2000

OK = "ok"
FAILED = "failed"


@dataclass(frozen=True)
class Follow:
    """What one phone follows: job id to ``notify``, ``quiet`` or ``off``; ``default``, what every job not listed
    does (``off`` unless the phone asked for more: a job made later then follows too); and whether any failed
    run notifies."""

    jobs: Dict[str, str]
    failures: bool = False
    default: str = "off"

    def mode(self, job_id: str) -> str:
        return self.jobs.get(job_id, self.default)

    def follows(self, job_id: str) -> bool:
        return self.mode(job_id) != "off"

    def wants(self, job_id: str, status: str) -> Optional[bool]:
        """None to send nothing, else whether the run should notify."""
        mode = self.mode(job_id)
        if status == FAILED and self.failures:
            return True
        if mode == "off":
            return None
        return mode == "notify"

    @staticmethod
    def parse(data: object) -> "Follow":
        if not isinstance(data, dict):
            raise ValueError("follow must be an object")
        jobs = data.get("jobs", {})
        if not isinstance(jobs, dict) or len(jobs) > MAX_JOBS:
            raise ValueError("jobs must be an object of at most %d jobs" % MAX_JOBS)
        default = data.get("default", "off")
        if default not in JOB_MODES:
            raise ValueError("default must be notify, quiet or off")
        clean = {}
        for job_id, mode in jobs.items():
            if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
                raise ValueError("bad job id")
            if mode not in JOB_MODES:
                raise ValueError("mode must be notify, quiet or off")
            # What the default already says needn't be kept.
            if mode != default:
                clean[job_id] = mode
        failures = data.get("failures", False)
        if not isinstance(failures, bool):
            raise ValueError("failures must be true or false")
        return Follow(clean, failures, default)

    def to_json(self) -> dict:
        return {"jobs": self.jobs, "failures": self.failures, "default": self.default}


class Follows:
    """Every phone's Follow, in ``cron.json`` beside the device list."""

    def __init__(self, store: Store):
        self.store = store

    @property
    def path(self) -> Path:
        return self.store.root / "cron.json"

    def all(self) -> Dict[str, Follow]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out = {}
        for device_id, entry in (raw.items() if isinstance(raw, dict) else ()):
            try:
                out[device_id] = Follow.parse(entry)
            except ValueError:
                continue
        return out

    def get(self, device_id: str) -> Follow:
        return self.all().get(device_id, Follow({}))

    def set(self, device_id: str, follow: Follow) -> None:
        if not DEVICE_ID_RE.match(device_id):
            raise ValueError("bad device_id")
        if self.store.get(device_id) is None:
            raise ValueError("not registered")
        with self.store._lock():
            data = {k: v.to_json() for k, v in self.all().items() if self.store.get(k) is not None}
            data[device_id] = follow.to_json()
            _write_private(self.path, data)


@dataclass(frozen=True)
class Run:
    profile: str
    job_id: str
    run: str
    name: str
    status: str
    text: str
    at: float


_HEADER = re.compile(r"^# Cron Job: (.+)$", re.M)
_STATUS = re.compile(r"^\*\*Status:\*\* (.+)$", re.M)
_MODE = re.compile(r"^\*\*Mode:\*\* (.+)$", re.M)
# The scheduler's own tag, always in capitals: a job named "Retry (failed)" is just a name.
_FAILED_TAG = re.compile(r"\s*\(FAILED\)$")
# The scheduler stamps the prompt's and the reply's lengths, so headings quoted inside either can't pass for its own.
_PROMPT_HEADING = "## Prompt\n\n"
_PROMPT_FRAME = re.compile(r"^\*\*Prompt Characters:\*\* (\d+)\n## Prompt\n\n", re.M)
_RESPONSE_FRAME = re.compile(r"\*\*Response Characters:\*\* (\d+)\n## Response\n\n")
# What an agent puts on the first line of its reply, by itself, to have the run recorded as failed.
FAILURE_MARKER = "[CRON_FAILURE]"
# What the scheduler logs for an agent that said nothing: a failure it doesn't deliver.
NO_RESPONSE = "(No response generated)"
# Terminal colours in a script's output: noise on a phone, and six bytes each once escaped into JSON.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _response(doc: str) -> Optional[str]:
    """The agent's reply, found the way the scheduler reads it back: by its length frame, or in a run written
    before frames, after the last ``## Response`` heading (the prompt above it may quote one). A framed run whose
    reply frame doesn't line up (cut short) has no reply."""
    prompt = _PROMPT_FRAME.search(doc)
    if prompt and doc.find(_PROMPT_HEADING) == prompt.end() - len(_PROMPT_HEADING):
        frame = _RESPONSE_FRAME.match(doc, prompt.end() + int(prompt.group(1)) + 2)
        return doc[frame.end():frame.end() + int(frame.group(1))] if frame else None
    _, heading, reply = doc.rpartition("\n## Response\n")
    # The heading is followed by one blank line; the reply's own first line is what the marker checks read.
    return (reply[1:] if reply.startswith("\n") else reply) if heading else None


def is_cron_silence(text: str) -> bool:
    """Whether the scheduler kept this reply to itself: Hermes' own rule for cron (a marker alone, on its own
    first or last line, or opening the reply), or the plain one where Hermes isn't importable."""
    try:
        from gateway.response_filters import is_autonomous_silence_response
        return is_autonomous_silence_response(text)
    except ImportError:
        pass
    from .watcher import _SILENCE, is_silence

    stripped = text.strip()
    lines = [line for line in stripped.splitlines() if line.strip()]
    bracketed = tuple(m for m in _SILENCE if m.startswith("["))
    return bool(stripped) and (stripped.upper().startswith(bracketed)
                               or any(is_silence(c) for c in (stripped, lines[0], lines[-1])))


def parse_run(doc: str) -> Optional[Tuple[str, str, str]]:
    """(job name, ok or failed, the words to show) from one output file; None for a run that stayed silent."""
    name_match = _HEADER.search(doc)
    title = (name_match.group(1).strip() if name_match else "")
    # The scheduler marks a failed run in its heading: "<name> (FAILED)", with the error last.
    failed = bool(_FAILED_TAG.search(title))
    name = _FAILED_TAG.sub("", title)[:MAX_LABEL]
    # Status and mode lines are the scheduler's only above the prompt or a script's output, which may quote them.
    header = re.split(r"\n## Prompt\n|\n---\n", doc, maxsplit=1)[0]
    status_match = _STATUS.search(header)
    status_line = status_match.group(1).strip().lower() if status_match else ""
    mode_match = _MODE.search(header)
    script = bool(mode_match and "no_agent" in mode_match.group(1))
    if status_line.startswith("silent"):
        return None

    if failed:
        _, heading, error = doc.rpartition("\n## Error\n")
        return name, FAILED, (error.strip().strip("`").strip() if heading else "") or "The run failed."
    if status_line.startswith(("blocked", "monitor source failed")):
        # "**Status:** BLOCKED\n\n<why>", or a monitor that couldn't read its source.
        reason = doc[status_match.end():].strip() if status_match else ""
        return name, FAILED, reason or "The run didn't run."
    response = _response(doc)
    if response is not None:
        lines = response.splitlines()
        if lines and lines[0].rstrip() == FAILURE_MARKER:
            return name, FAILED, "\n".join(lines[1:]).strip() or "The agent reported the run failed."
        response = response.strip()
        if response == NO_RESPONSE:
            return name, FAILED, "The agent gave no response."
        if not response or is_cron_silence(response):
            return None
        return name, OK, response

    if script:
        if status_line.startswith("script failed"):
            # "**Status:** script failed\n\n<output>": the script's own words say why.
            reason = doc[status_match.end():].strip() if status_match else ""
            return name, FAILED, reason or "The script failed."
        # A script's output sits between the header and the closing rule.
        body = doc.split("\n---\n", 1)[1] if "\n---\n" in doc else ""
        body = body.rsplit("\n---", 1)[0].strip() if body.rstrip().endswith("---") else body.strip()
        if not body or is_cron_silence(body):
            return None
        return name, OK, body

    # The scheduler's last-ditch form: "# Cron Job: <name>\n\nError: <exc>".
    tail = doc[name_match.end():].strip() if name_match else doc.strip()
    if tail.startswith("Error:"):
        return name, FAILED, tail[len("Error:"):].strip() or "The run failed."
    return None


def output_dirs(homes: Iterable[Tuple[str, Path]]) -> Iterable[Tuple[str, str, Path]]:
    """(profile, job id, the job's output folder) for every job with output, in every profile."""
    for profile, home in homes:
        root = home / "cron" / "output"
        if not PROFILE_RE.match(profile) or not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for folder in entries:
            if folder.is_dir() and JOB_ID_RE.match(folder.name):
                yield profile, folder.name, folder


def runs_after(folder: Path, after: str) -> List[Path]:
    """The job's output files named after ``after``, oldest first."""
    try:
        files = [p for p in folder.iterdir() if p.suffix == ".md" and RUN_RE.match(p.stem) and p.stem > after]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.stem)


def newest_run(folder: Path) -> str:
    runs = runs_after(folder, "")
    return runs[-1].stem if runs else ""


def zone_of(profile: str, home: Path) -> Optional[tzinfo]:
    """The time zone a profile's scheduler names its runs in: the profile's own ``timezone`` setting; None for
    the machine's, Hermes' fallback too. One gateway runs every profile's jobs, each on its profile's clock, so
    ``HERMES_TIMEZONE`` (the default profile's, copied from its config at startup) speaks for that one only."""
    try:
        try:
            import hermes_yaml as yaml
        except ImportError:
            import yaml
        data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        value = data.get("timezone") if isinstance(data, dict) else None
        name = value.strip() if isinstance(value, str) else ""
    except Exception:
        name = ""
    if not name and profile == "default":
        name = os.getenv("HERMES_TIMEZONE", "").strip()
    try:
        return ZoneInfo(name) if name else None
    except Exception:  # a zone this machine can't load
        return None


def run_time(run: str, zone: Optional[tzinfo] = None) -> float:
    """When a run started, from its name on its scheduler's clock: not the file's mtime, which a copy or a
    restored backup resets."""
    started = datetime.strptime(run, "%Y-%m-%d_%H-%M-%S")
    return started.replace(tzinfo=zone).timestamp() if zone is not None else time.mktime(started.timetuple())


def read_run(profile: str, job_id: str, path: Path, zone: Optional[tzinfo] = None) -> Optional[Run]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            doc = fh.read(MAX_READ)
        at = run_time(path.stem, zone)
    except (OSError, ValueError):
        return None
    parsed = parse_run(doc)
    if parsed is None:
        return None
    name, status, text = parsed
    return Run(profile, job_id, path.stem, name or job_id, status, _ANSI.sub("", text), at)


class CronWatcher:
    """Finds runs finished since the last look. Its marks (job folder to newest run seen) live with the
    Bot Chat watcher's, under ``cron:<profile>:<job>`` keys."""

    def __init__(self, now=time.time):
        self.now = now
        self._polled = False

    def poll(self, homes: Iterable[Tuple[str, Path]], marks: Dict[str, str]) -> Tuple[List[Run], bool]:
        found: List[Run] = []
        changed = False
        # Only the first look of a watcher that never kept marks (the plugin just installed) takes every job's
        # history as old news. After that a folder that appears is a new job, and its first run is news;
        # one made while the gateway was down is read too, and staleness keeps its old runs out.
        baseline = not self._polled and not marks
        self._polled = True
        homes = list(homes)
        for profile, job_id, folder in output_dirs(homes):
            key = f"cron:{profile}:{job_id}"
            if key not in marks:
                marks[key] = newest_run(folder) if baseline else ""
                changed = True
                if baseline:
                    continue
            for path in runs_after(folder, marks[key]):
                try:
                    age = self.now() - path.stat().st_mtime
                except OSError:
                    continue
                if age < SETTLE_SECONDS:
                    break
                marks[key] = path.stem
                changed = True
                if age > STALE_SECONDS:
                    continue
                run = read_run(profile, job_id, path, zone_of(profile, folder.parent.parent.parent))
                if run is not None:
                    found.append(run)
        # A deleted job's mark goes with it: only when its profile's output was there to look in, so a folder
        # that couldn't be read for a moment doesn't come back as a new job and send its runs again.
        outputs = {profile: home / "cron" / "output" for profile, home in homes}
        for key in list(marks):
            parts = key.split(":", 2)
            root = outputs.get(parts[1]) if len(parts) == 3 else None
            if root is not None and root.is_dir() and not (root / parts[2]).exists():
                del marks[key]
                changed = True
        return found, changed


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def as_item(run: Run, limit: int) -> dict:
    return {"bot": run.profile, "job": run.job_id, "run": run.run, "label": run.name, "status": run.status,
            "at": int(run.at), "text": _clip(run.text, limit)}


def inbox(homes: Iterable[Tuple[str, Path]], follow: Follow, limit: int = 50, before: float = 0) -> List[dict]:
    """The runs a phone follows (and every failure, when it asked), newest first, each with the start of its
    text. ``before`` (unix seconds) pages back: a page ends with every run of its last second, so the next
    page, before that second, leaves none out."""
    limit = max(1, min(int(limit), MAX_LIST))
    # Runs are ordered by their names' times, so only the files that make the page are read (the scheduler
    # keeps a job's last 50 runs by default; MAX_SCAN bounds a gateway that keeps them all).
    candidates: List[Tuple[float, str, str, bool, Path, Optional[tzinfo]]] = []
    zones: Dict[str, Optional[tzinfo]] = {}
    for profile, job_id, folder in output_dirs(homes):
        followed = follow.follows(job_id)
        if not followed and not follow.failures:
            continue
        if profile not in zones:
            zones[profile] = zone_of(profile, folder.parent.parent.parent)
        for path in runs_after(folder, ""):
            try:
                at = run_time(path.stem, zones[profile])
            except ValueError:
                continue
            if not before or at < before:
                candidates.append((at, profile, job_id, followed, path, zones[profile]))
    candidates.sort(key=lambda c: c[0], reverse=True)
    found: List[dict] = []
    for at, profile, job_id, followed, path, zone in candidates[:MAX_SCAN]:
        if len(found) >= limit and int(at) != found[-1]["at"]:
            break
        run = read_run(profile, job_id, path, zone)
        if run is not None and (followed or run.status == FAILED):
            found.append(as_item(run, LIST_TEXT))
    return found


def read(homes: Iterable[Tuple[str, Path]], profile: str, job_id: str, run: str) -> Optional[dict]:
    """One run in full (up to MAX_TEXT), or None when there's no such run or it stayed silent."""
    if not (PROFILE_RE.match(profile) and JOB_ID_RE.match(job_id) and RUN_RE.match(run)):
        return None
    for name, home in homes:
        if name != profile:
            continue
        path = home / "cron" / "output" / job_id / f"{run}.md"
        found = read_run(profile, job_id, path, zone_of(profile, home)) if path.is_file() else None
        return as_item(found, MAX_TEXT) if found else None
    return None
