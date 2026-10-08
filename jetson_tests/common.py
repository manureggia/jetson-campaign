"""Durable files and provenance shared by local orchestration and remote worker."""
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def append_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


PHASES = ["PENDING", "PRECHECK", "SETUP", "WARMUP", "SNAPSHOT_BEFORE",
          "START_MONITORS", "START_WORKLOADS", "RUNNING", "STOP_WORKLOADS",
          "STOP_MONITORS", "SNAPSHOT_AFTER", "PROCESS_RESULTS", "VALIDATE", "COMPLETE"]


class RunState:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.value = read_json(self.directory / "status.json", {"phase": "PENDING", "outcome": "INCOMPLETE"})

    def transition(self, phase, **fields):
        old = self.value["phase"]
        allowed = (phase in {"FAILED", "CLEANUP"} or
                   (old in PHASES and PHASES.index(old) + 1 < len(PHASES)
                    and PHASES[PHASES.index(old) + 1] == phase) or
                   (old == "VALIDATE" and phase == "SETUP") or
                   (old in {"RUNNING", "STOP_MONITORS"} and phase == "RETRY_MONITORS") or
                   (old == "RETRY_MONITORS" and phase == "SNAPSHOT_BEFORE") or
                   (old == "CLEANUP" and phase == "COMPLETE"))
        if not allowed:
            raise ValueError(f"Invalid transition {old} -> {phase}")
        self.value.update(fields, phase=phase, timestamp=timestamp(), monotonic=time.monotonic())
        append_json(self.directory / "transitions.jsonl", {"from": old, **self.value})
        save_json(self.directory / "status.json", self.value)


def attempt_relative(item, attempt):
    return f"core{item['core']}/{item['scenario']}/{item['kind']}_{item['run']:03d}/attempt_{attempt:03d}"


def choose_attempt(attempts, rerun_failed=False, force=False):
    """First valid attempt wins, unless an explicit force starts a new generation."""
    if not force and any(a.get("outcome") == "PASS" for a in attempts):
        return "skip"
    if attempts and attempts[-1].get("outcome") == "FAIL" and not (rerun_failed or force):
        return "skip_failed"
    return "run"
