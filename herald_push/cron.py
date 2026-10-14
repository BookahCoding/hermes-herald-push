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
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

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
# How far back each job's folder is looked through for the Inbox list.
PER_JOB = 30

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
        self._lock = threading.Lock()

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
        with self._lock, self.store._lock():
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


def _section(doc: str, title: str) -> Optional[str]:
    """The text under ``## <title>`` up to the next level-2 heading the scheduler wrote, or the end."""
    match = re.search(r"^## " + re.escape(title) + r"[ \t]*\n", doc, re.M)
    if not match:
        return None
    return doc[match.end():].strip()


def parse_run(doc: str) -> Optional[Tuple[str, str, str]]:
    """(job name, ok or failed, the words to show) from one output file; None for a run that stayed silent."""
    from .watcher import is_silence

    name_match = _HEADER.search(doc)
    name = (name_match.group(1).strip() if name_match else "")
    # The scheduler marks a failed run in the heading too: "<name> (FAILED)".
    name = re.sub(r"\s*\((FAILED|failed)\)$", "", name)[:MAX_LABEL]
    status_match = _STATUS.search(doc)
    status_line = status_match.group(1).strip().lower() if status_match else ""
    mode_match = _MODE.search(doc)
    script = bool(mode_match and "no_agent" in mode_match.group(1))
    if status_line.startswith("silent"):
        return None

    error = _section(doc, "Error")
    if error is not None:
        return name, FAILED, error.strip("`\n ").strip() or "The run failed."
    response = _section(doc, "Response")
    if response is not None:
        if not response or is_silence(response):
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
        if not body or is_silence(body):
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


def read_run(profile: str, job_id: str, path: Path) -> Optional[Run]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            doc = fh.read(MAX_READ)
        at = path.stat().st_mtime
    except OSError:
        return None
    parsed = parse_run(doc)
    if parsed is None:
        return None
    name, status, text = parsed
    return Run(profile, job_id, path.stem, name or job_id, status, text, at)


class CronWatcher:
    """Finds runs finished since the last look. Its marks (job folder to newest run seen) live with the
    Bot Chat watcher's, under ``cron:<profile>:<job>`` keys."""

    def __init__(self, now=time.time):
        self.now = now

    def poll(self, homes: Iterable[Tuple[str, Path]], marks: Dict[str, str]) -> Tuple[List[Run], bool]:
        found: List[Run] = []
        changed = False
        for profile, job_id, folder in output_dirs(homes):
            key = f"cron:{profile}:{job_id}"
            if key not in marks:
                # A job seen for the first time starts from now: its history isn't news.
                marks[key] = newest_run(folder)
                changed = True
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
                run = read_run(profile, job_id, path)
                if run is not None:
                    found.append(run)
        return found, changed


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def as_item(run: Run, limit: int) -> dict:
    return {"bot": run.profile, "job": run.job_id, "run": run.run, "label": run.name, "status": run.status,
            "at": int(run.at), "text": _clip(run.text, limit)}


def inbox(homes: Iterable[Tuple[str, Path]], follow: Follow, limit: int = 50, before: float = 0) -> List[dict]:
    """The runs a phone follows (and every failure, when it asked), newest first, each with the start of its
    text. ``before`` (unix seconds) pages back."""
    limit = max(1, min(int(limit), MAX_LIST))
    found: List[Run] = []
    for profile, job_id, folder in output_dirs(homes):
        followed = follow.follows(job_id)
        if not followed and not follow.failures:
            continue
        for path in reversed(runs_after(folder, "")[-PER_JOB:]):
            run = read_run(profile, job_id, path)
            if run is None or (before and run.at >= before):
                continue
            if followed or run.status == FAILED:
                found.append(run)
    found.sort(key=lambda r: r.at, reverse=True)
    return [as_item(r, LIST_TEXT) for r in found[:limit]]


def read(homes: Iterable[Tuple[str, Path]], profile: str, job_id: str, run: str) -> Optional[dict]:
    """One run in full (up to MAX_TEXT), or None when there's no such run or it stayed silent."""
    if not (PROFILE_RE.match(profile) and JOB_ID_RE.match(job_id) and RUN_RE.match(run)):
        return None
    for name, home in homes:
        if name != profile:
            continue
        path = home / "cron" / "output" / job_id / f"{run}.md"
        found = read_run(profile, job_id, path) if path.is_file() else None
        return as_item(found, MAX_TEXT) if found else None
    return None
