"""SSH and rsync transport; the alias is fixed by the workspace policy."""
import io
import json
import shlex
import subprocess
import tarfile
import time
from pathlib import Path

from .common import append_json, file_hash, read_json, timestamp

ALIAS = "jetson-codex"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", ALIAS]


class Transport:
    def __init__(self, logs):
        self.logs = Path(logs)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.sequence = time.time_ns()

    def run(self, argv, input=None, timeout=240, check=True):
        self.sequence += 1
        label = str(self.sequence)
        before = time.monotonic()
        record = {"id": label, "timestamp": timestamp(), "host": ALIAS, "argv": argv}
        append_json(self.logs / "commands.jsonl", {"event": "start", **record})
        try:
            result = subprocess.run(argv, input=input, capture_output=True, timeout=timeout)
            code, stdout, stderr = result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            code, stdout, stderr = None, exc.stdout or b"", exc.stderr or b""
        (self.logs / f"{label}.stdout.txt").write_bytes(stdout)
        (self.logs / f"{label}.stderr.txt").write_bytes(stderr)
        append_json(self.logs / "commands.jsonl", {"event": "end", "id": label, "exit_code": code,
                     "duration_s": time.monotonic() - before, "timestamp": timestamp(),
                     "stdout": label + ".stdout.txt", "stderr": label + ".stderr.txt"})
        if check and code != 0:
            raise RuntimeError(f"Transport command failed ({code}): {stderr.decode(errors='replace')[-2000:]}")
        return code, stdout, stderr

    def ssh(self, command, **kwargs):
        return self.run([*SSH, command], **kwargs)

    def upload(self, root, config_dir, repository):
        self.ssh(shlex.join(["mkdir", "-p", root + "/code", root + "/sources"]))
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for relative in ("jetson_tests", "tools/FlameGraph"):
                for path in sorted((repository / relative).rglob("*")):
                    if path.is_file() and "__pycache__" not in path.parts:
                        archive.add(path, arcname="code/" + str(path.relative_to(repository)))
            helper = repository / "tools/jetson-campaign-cgroup"
            archive.add(helper, arcname="code/tools/jetson-campaign-cgroup")
            for filename in ("config.json", "provenance.json"):
                archive.add(config_dir / filename, arcname=filename)
            for path in (config_dir / "sources").glob("*.yaml"):
                archive.add(path, arcname="sources/" + path.name)
        self.ssh(shlex.join(["tar", "-xf", "-", "-C", root]), input=buffer.getvalue())

    def put_json(self, path, value):
        # Atomic write via the remote stdlib. Path is a quoted argv value, never shell text from an LLM.
        script = "import os,sys; p=sys.argv[1]; t=p+'.upload'; f=open(t,'wb'); f.write(sys.stdin.buffer.read()); f.close(); os.replace(t,p)"
        self.ssh(shlex.join(["python3", "-c", script, path]), input=json.dumps(value).encode())

    def worker(self, root, operation, **kwargs):
        argv = ["python3", "-m", "jetson_tests.worker", operation, "--root", root]
        for key, value in kwargs.items():
            argv += ["--" + key.replace("_", "-"), str(value)]
        _, data, _ = self.ssh("cd " + shlex.quote(root + "/code") + " && " + shlex.join(argv), timeout=300)
        return json.loads(data)

    def fetch(self, remote, local):
        local = Path(local)
        local.mkdir(parents=True, exist_ok=True)
        self.run(["rsync", "-az", "--partial", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=8",
                  ALIAS + ":" + remote.rstrip("/") + "/", str(local) + "/"], timeout=3600)


def verify_download(directory):
    directory = Path(directory)
    manifest = read_json(directory / "manifest.json")
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("Missing final manifest")
    for name, expected in manifest.items():
        path = (directory / name).resolve()
        if directory.resolve() not in path.parents or not path.is_file() or file_hash(path) != expected:
            raise ValueError(f"Invalid or mismatched downloaded artifact: {name}")
    return manifest
