import io
import asyncio
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from self_heal.daytona_transport import DaytonaTransport


ROOT = Path(__file__).resolve().parents[1]


class FakeProcess:
    def __init__(self):
        self.commands = []
        self.inputs = []
        self.logs = []

    def exec(self, command, **kwargs):
        self.commands.append(command)
        return SimpleNamespace(exit_code=0)

    def create_session(self, session_id, **kwargs):
        self.session_id = session_id

    def execute_session_command(self, session_id, request, **kwargs):
        self.request = request
        return SimpleNamespace(cmd_id="command-1")

    def send_session_command_input(self, session_id, command_id, data, **kwargs):
        self.inputs.append(json.loads(data))

    async def get_session_command_logs_async(
        self, session_id, command_id, on_stdout, on_stderr
    ):
        for chunk in self.logs:
            on_stdout(chunk)
        await asyncio.Event().wait()


def make_runner():
    process = FakeProcess()
    sandbox = SimpleNamespace(id="sandbox-1", process=process)
    uploaded = []
    sandbox.fs = SimpleNamespace(
        upload_file=lambda src, dst, **kwargs: uploaded.append(src)
    )

    class Client:
        def __init__(self):
            self.deleted = []

        def create(self, params, **kwargs):
            self.params = params
            return sandbox

        def delete(self, value, **kwargs):
            self.deleted.append(value.id)

    client = Client()

    class AsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, identifier):
            return sandbox

    runner = SimpleNamespace(
        repository=ROOT,
        async_daytona_factory=AsyncClient,
        snapshot=lambda: SimpleNamespace(id="immutable-snapshot"),
        daytona=lambda: client,
        last_sandbox_id=None,
        cleanup_error=None,
    )
    return runner, client, process, uploaded


def test_stage_only_harness_and_runtime_with_no_credentials_or_oracle():
    runner, client, process, uploaded = make_runner()
    with DaytonaTransport(runner, ROOT / "harness") as transport:
        transport.send({"type": "initial"})
        assert client.params.snapshot == "immutable-snapshot"
        assert client.params.network_block_all is True
        assert client.params.env_vars == {}
        assert client.params.ttl_minutes == 10
        assert process.request.suppress_input_echo is True
        assert process.request.run_async is True
        assert (
            "env -i" in process.request.command and "nobody" in process.request.command
        )
        with zipfile.ZipFile(io.BytesIO(uploaded[0])) as bundle:
            names = bundle.namelist()
            assert "harness/agent.py" in names and "container_main.py" in names
            assert not any(
                name.startswith(("evals/", "tests/", ".git/", ".seal/"))
                or ".env" in name
                for name in names
            )
            assert "self_heal/storage.py" not in names
    assert client.deleted == ["sandbox-1"]


def test_streamed_partial_logs_yield_each_rpc_once():
    runner, client, process, _ = make_runner()
    process.logs = ['{"type":"ta', 'ble"}\n{"type":"result"}\n']
    with DaytonaTransport(runner, ROOT / "harness") as transport:
        assert transport.receive(1) == {"type": "table"}
        assert transport.receive(1) == {"type": "result"}
    assert client.deleted == ["sandbox-1"]


def test_timeout_cleans_up_sandbox():
    runner, client, _, _ = make_runner()
    with pytest.raises(TimeoutError):
        with DaytonaTransport(runner, ROOT / "harness") as transport:
            transport.receive(0)
    assert client.deleted == ["sandbox-1"]


def test_setup_failure_also_cleans_up():
    runner, client, process, _ = make_runner()
    process.exec = lambda *args, **kwargs: SimpleNamespace(exit_code=1)
    with pytest.raises(RuntimeError, match="staging"):
        with DaytonaTransport(runner, ROOT / "harness"):
            pass
    assert client.deleted == ["sandbox-1"]


def test_cleanup_failure_is_explicit_and_cannot_report_success():
    runner, client, _, _ = make_runner()

    def fail(*args, **kwargs):
        raise ConnectionError("offline")

    client.delete = fail
    with pytest.raises(RuntimeError, match="cleanup"):
        with DaytonaTransport(runner, ROOT / "harness"):
            pass
    assert runner.cleanup_error == "ConnectionError"


def test_oversized_rpc_is_rejected():
    runner, _, process, _ = make_runner()
    process.logs = ["x" * 2_000_001]
    with pytest.raises(ValueError, match="size limit"):
        with DaytonaTransport(runner, ROOT / "harness") as transport:
            transport.receive(1)
