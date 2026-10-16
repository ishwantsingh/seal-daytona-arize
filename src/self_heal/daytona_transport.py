"""Daytona sandbox lifecycle and bounded, credential-free supervisor RPC."""

from __future__ import annotations

import io
import asyncio
import queue
import threading
import json
import shlex
import time
import uuid
import zipfile
from pathlib import Path

from daytona import AsyncDaytona, CreateSandboxFromSnapshotParams, SessionExecuteRequest


class DaytonaTransport:
    def __init__(self, runner, harness_dir: Path):
        self.runner, self.harness_dir = runner, harness_dir
        self.sandbox = None
        self.session_id = "seal-" + uuid.uuid4().hex
        self.offset = 0
        self.chunks = queue.Queue(maxsize=64)
        self.stream_error = None
        self.loop = None
        self.stream_task = None
        self.thread = None
        self.pending = ""

    def __enter__(self):
        try:
            snapshot = self.runner.snapshot()
            self.sandbox = self.runner.daytona().create(
                CreateSandboxFromSnapshotParams(
                    snapshot=snapshot.id,
                    os_user="root",
                    network_block_all=True,
                    env_vars={},
                    labels={"app": "seal", "purpose": "candidate-evaluation"},
                    auto_stop_interval=5,
                    auto_delete_interval=10,
                    ttl_minutes=10,
                ),
                timeout=60,
            )
            self.runner.last_sandbox_id = self.sandbox.id
            # Stage only reviewed runtime shims and the editable harness. No repo,
            # credentials, SQLite file, tests, or oracle is uploaded.
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(self.harness_dir.rglob("*.py")):
                    if path.is_symlink():
                        raise ValueError("Candidate symlinks are forbidden")
                    bundle.write(
                        path, "harness/" + path.relative_to(self.harness_dir).as_posix()
                    )
                for path in sorted(
                    (self.runner.repository / "runner_support").rglob("*.py")
                ):
                    bundle.write(
                        path,
                        path.relative_to(
                            self.runner.repository / "runner_support"
                        ).as_posix(),
                    )
            uploaded = "/tmp/" + self.session_id + ".zip"
            self.sandbox.fs.upload_file(archive.getvalue(), uploaded, timeout=30)
            setup = (
                "import zipfile; zipfile.ZipFile("
                + repr(uploaded)
                + ").extractall('/opt/seal')"
            )
            result = self.sandbox.process.exec(
                "python -c "
                + shlex.quote(setup)
                + " && chmod -R a-w /opt/seal && rm "
                + shlex.quote(uploaded),
                timeout=30,
            )
            if result.exit_code:
                raise RuntimeError("Daytona runtime staging failed")
            self.sandbox.process.create_session(self.session_id, request_timeout=10)
            command = "su -s /bin/sh nobody -c " + shlex.quote(
                "env -i PATH=/usr/local/bin:/usr/bin:/bin PYTHONPATH=/opt/seal PYTHONDONTWRITEBYTECODE=1 python -u /opt/seal/container_main.py"
            )
            response = self.sandbox.process.execute_session_command(
                self.session_id,
                SessionExecuteRequest(
                    command=command, run_async=True, suppress_input_echo=True
                ),
                timeout=10,
            )
            self.command_id = response.cmd_id
            self.thread = threading.Thread(
                target=self._stream_logs, daemon=True, name="seal-daytona-logs"
            )
            self.thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def send(self, message):
        self.sandbox.process.send_session_command_input(
            self.session_id,
            self.command_id,
            json.dumps(message, separators=(",", ":")) + "\n",
            request_timeout=10,
        )

    def _stream_logs(self):
        async def follow():
            self.loop = asyncio.get_running_loop()
            self.stream_task = asyncio.current_task()

            def stdout(chunk):
                self.chunks.put_nowait(chunk)

            try:
                factory = (
                    getattr(self.runner, "async_daytona_factory", None) or AsyncDaytona
                )
                async with factory() as client:
                    sandbox = await client.get(self.sandbox.id)
                    await sandbox.process.get_session_command_logs_async(
                        self.session_id, self.command_id, stdout, lambda chunk: None
                    )
                self.stream_error = RuntimeError(
                    "Daytona log stream ended before the result"
                )
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.stream_error = exc

        asyncio.run(follow())

    def receive(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                if len(line.encode()) > 2_000_000:
                    raise ValueError("Candidate bridge message exceeded size limit")
                return json.loads(line)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Task time budget exceeded")
            try:
                chunk = self.chunks.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                if self.stream_error is not None:
                    raise self.stream_error
                continue
            self.pending += chunk
            self.offset += len(chunk.encode())
            if len(self.pending.encode()) > 2_000_000 or self.offset > 16_000_000:
                raise ValueError("Candidate bridge output exceeded size limit")

    def close(self):
        if (
            self.loop is not None
            and self.stream_task is not None
            and self.loop.is_running()
        ):
            self.loop.call_soon_threadsafe(self.stream_task.cancel)
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.sandbox is not None:
            sandbox, self.sandbox = self.sandbox, None
            try:
                self.runner.daytona().delete(sandbox, timeout=30, wait=True)
            except Exception as exc:
                self.runner.cleanup_error = type(exc).__name__
                # TTL/auto-delete is a second cleanup mechanism, never a silent success.
                raise RuntimeError("Daytona sandbox cleanup failed") from exc

    def __exit__(self, *args):
        self.close()
