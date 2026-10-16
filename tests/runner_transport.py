"""Test-only local process transport. Never available to production CLI."""

import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class LocalProcessTransport:
    def __init__(self, runner, harness_dir):
        self.runner, self.harness_dir = runner, harness_dir
        self.pending = b""

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        shutil.copytree(
            self.runner.repository / "runner_support", root, dirs_exist_ok=True
        )
        shutil.copytree(self.harness_dir, root / "harness")
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(root / "container_main.py")],
            cwd=root,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(root),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        return self

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def receive(self, timeout):
        while b"\n" not in self.pending:
            if not self.selector.select(timeout):
                raise TimeoutError("Process bridge timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError(self.process.stderr.read().decode())
            self.pending += chunk
        line, self.pending = self.pending.split(b"\n", 1)
        return json.loads(line)

    def __exit__(self, *args):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.process.stderr.close()
        self.selector.close()
        self.directory.cleanup()
