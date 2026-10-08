"""Owned process groups, bounded commands and observed Linux affinity.

Based on the start_new_session/killpg approach in run_timer_diagnostics.py.
Commands must stay in the foreground (no daemonizing or setsid in profiles).
"""
import os
import signal
import subprocess
import time
from pathlib import Path

from .common import append_json, save_json, timestamp


class RunFailure(RuntimeError):
    def __init__(self, message, category="deterministic"):
        super().__init__(message)
        self.category = category


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_info(pid):
    try:
        data = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return {"pid": int(pid), "state": data[0], "ppid": int(data[1]),
                "pgid": int(data[2]), "sid": int(data[3]), "start": data[19]}
    except (OSError, ValueError, IndexError):
        return None


def process_table():
    return [info for p in Path("/proc").glob("[0-9]*") if (info := process_info(p.name))]


def matches(identity):
    current = process_info(identity["pid"])
    return current and current["start"] == identity["start"]


def thread_ids(pid):
    # procfs can expose a zero entry while a thread exits. sched_*(0)
    # queries the caller, so it must never be used to inspect this process.
    for path in Path(f"/proc/{pid}/task").glob("[0-9]*"):
        tid = int(path.name)
        if tid > 0:
            yield tid


class Processes:
    def __init__(self, directory, deadline, shutdown_s=10):
        self.directory = Path(directory)
        self.deadline = deadline
        self.shutdown_s = shutdown_s
        self.jobs = []
        self.last_inventory = 0

    def spawn(self, name, argv, cwd=None, env=None, cpus=None, required=False, output_dir=None):
        output_dir = Path(output_dir or self.directory)
        output_dir.mkdir(parents=True, exist_ok=True)
        stdout, stderr = output_dir / f"{name}.stdout.txt", output_dir / f"{name}.stderr.txt"
        command_id = f"{len(self.jobs):04d}_{name}"
        command = argv
        while len(command) > 3 and Path(command[0]).name == "taskset" and command[1] == "-c":
            command = command[3:]
        if Path(command[0]).name == "perf":
            # Decimal commas collide with perf stat's CSV separator.
            env = {**(os.environ if env is None else env), "LC_ALL": "C"}
        if cpus is not None:
            argv = ["taskset", "-c", ",".join(map(str, cpus)), *argv]
        start = time.monotonic()
        with stdout.open("wb") as out, stderr.open("wb") as err:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, start_new_session=True)
        identity = process_info(proc.pid)
        if identity is None:
            proc.wait()
            raise RunFailure(f"Could not identify newly launched {name}")
        record = {"id": command_id, "name": name, "argv": argv, "cwd": str(cwd or Path.cwd()),
                  "host": os.uname().nodename, "started": timestamp(), "start_monotonic": start,
                  "identity": identity, "boot_id": boot_id(),
                  "stdout": str(stdout), "stderr": str(stderr)}
        job = dict(proc=proc, record=record, known={proc.pid: identity}, cpus=cpus,
                   required=required, logged=False)
        self.jobs.append(job)
        append_json(self.directory / "commands.jsonl", {"event": "start", **record})
        self.persist()
        return job

    def persist(self):
        save_json(self.directory / "processes.json", [j["record"] | {"known": list(j["known"].values())}
                                                     for j in self.jobs])

    def refresh(self):
        rows = process_table()
        for job in self.jobs:
            root = job["record"]["identity"]
            live = {pid for pid, known in job["known"].items() if matches(known)}
            root_now = process_info(root["pid"])
            # A Linux session holds its ID until its last member exits. This also finds
            # children orphaned before the next inventory after the leader exited.
            if root_now is None or root_now["start"] == root["start"]:
                live |= {r["pid"] for r in rows if r["sid"] == root["sid"]}
            for _ in range(len(rows) + 1):
                children = {r["pid"] for r in rows if r["ppid"] in live}
                if children <= live:
                    break
                live |= children
            for row in rows:
                if row["pid"] in live:
                    job["known"][row["pid"]] = row
                    if row["sid"] != root["sid"]:
                        raise RunFailure(f"{job['record']['name']} daemonized; foreground commands required")
        self.persist()

    def finish_log(self, job):
        code = job["proc"].poll()
        if code is not None and not job["logged"]:
            append_json(self.directory / "commands.jsonl", {"event": "end", "id": job["record"]["id"],
                         "timestamp": timestamp(), "exit_code": code,
                         "duration_s": time.monotonic() - job["record"]["start_monotonic"]})
            job["logged"] = True
        return code

    def check(self):
        if time.monotonic() >= self.deadline:
            raise RunFailure("Worker hard timeout", "timeout")
        if time.monotonic() - self.last_inventory >= 1:
            self.refresh()
            self.check_affinity()
            self.last_inventory = time.monotonic()
        for job in self.jobs:
            code = self.finish_log(job)
            if job["required"] and code is not None:
                raise RunFailure(f"Required workload {job['record']['name']} exited ({code})", "exit_code")

    def check_affinity(self):
        observed = []
        for job in self.jobs:
            if job["cpus"] is None:
                continue
            allowed = set(job["cpus"])
            for pid, identity in job["known"].items():
                if not matches(identity):
                    continue
                for tid in thread_ids(pid):
                    try:
                        mask = sorted(os.sched_getaffinity(tid))
                        row = {"name": job["record"]["name"], "pid": pid, "tid": tid,
                               "cpus": mask, "policy": os.sched_getscheduler(tid),
                               "priority": os.sched_getparam(tid).sched_priority}
                        observed.append(row)
                        if not mask or not set(mask) <= allowed:
                            raise RunFailure(f"Affinity mismatch: {row}", "affinity")
                    except ProcessLookupError:
                        continue
        append_json(self.directory / "affinity.jsonl", {"timestamp": timestamp(), "threads": observed})

    def wait(self, job, timeout=None):
        end = min(self.deadline, time.monotonic() + timeout) if timeout is not None else self.deadline
        while job["proc"].poll() is None:
            self.check()
            if time.monotonic() >= end:
                self.stop(job)
                raise RunFailure(f"Command timeout: {job['record']['name']}", "timeout")
            time.sleep(.1)
        self.finish_log(job)
        return job["proc"].returncode

    def sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(min(.2, max(0, end - time.monotonic())))

    def live_members(self, job):
        return [info for identity in job["known"].values()
                if (info := process_info(identity["pid"])) and info["start"] == identity["start"]
                and info["state"] != "Z"]

    def stop(self, job):
        job["required"] = False
        # Keep the group leader unreaped until the first signal when possible.
        try:
            self.refresh()
        except RunFailure:
            pass
        root = job["record"]["identity"]
        for sig in (signal.SIGTERM, signal.SIGKILL):
            live = self.live_members(job)
            if not live:
                break
            if any(x["pgid"] == root["pgid"] and x["sid"] == root["sid"] for x in live):
                try:
                    os.killpg(root["pgid"], sig)
                except ProcessLookupError:
                    pass
            # Also clean up individually identified children that escaped the group.
            for identity in live:
                if matches(identity) and identity["pgid"] != root["pgid"]:
                    try:
                        os.kill(identity["pid"], sig)
                    except ProcessLookupError:
                        pass
            deadline = time.monotonic() + (self.shutdown_s if sig == signal.SIGTERM else 2)
            while self.live_members(job) and time.monotonic() < deadline:
                time.sleep(.1)
        try:
            job["proc"].wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise RunFailure(f"Cleanup failed for {root}", "cleanup") from exc
        self.finish_log(job)
        if self.live_members(job):
            raise RunFailure(f"Surviving owned descendants for {root}", "cleanup")

    def cleanup(self):
        errors = []
        for job in reversed(self.jobs):
            try:
                self.stop(job)
            except (OSError, RunFailure) as exc:
                errors.append(str(exc))
        if errors:
            raise RunFailure("; ".join(errors), "cleanup")
