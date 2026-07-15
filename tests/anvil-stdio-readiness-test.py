#!/usr/bin/env python3
"""Deterministic readiness and atomic-dispatch regressions for anvil-stdio."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def make_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def percent_wire(document: dict[str, object]) -> str:
    return "".join(
        f"%{byte:02X}"
        for byte in json.dumps(
            document, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )


def read_count(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0


def write_fake_emacsclient(path: Path) -> None:
    source = r"""#!__PYTHON__
import base64
import os
from pathlib import Path
import re
import signal
import sys


def bump(name):
    path = Path(os.environ[name])
    try:
        value = int(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = 0
    value += 1
    path.write_text(str(value), encoding="utf-8")
    return value


expression = sys.argv[-1]
if os.environ.get("FAKE_EXPRESSION"):
    Path(os.environ["FAKE_EXPRESSION"]).write_text(expression, encoding="utf-8")
ready_prefix = (
    "(if (and (fboundp 'anvil-headless--ready-p) "
    '(anvil-headless--ready-p "anvil")) '
)
sentinel_suffix = '"anvil-mcp-headless-not-ready")'


def guarded_true_branch():
    if not (
        expression.startswith(ready_prefix)
        and expression.endswith(sentinel_suffix)
    ):
        return None
    branch = expression[len(ready_prefix) : -len(sentinel_suffix)]
    if not branch.endswith(" "):
        return None
    return branch[:-1]


def atomically_guards(body):
    branch = guarded_true_branch()
    if branch is None:
        return False
    if body != "anvil-server-process-jsonrpc":
        return branch == f'(progn {body} "anvil-mcp-lifecycle-complete")'
    inline_call = (
        "(encode-coding-string (or (anvil-server-process-jsonrpc "
        '(base64-decode-string "'
    )
    file_call = (
        "(encode-coding-string (or "
        '(anvil-server-process-jsonrpc anvil-request "anvil")'
    )
    return (
        branch.count("(anvil-server-process-jsonrpc ") == 1
        and (
            (branch.startswith("(mapconcat ") and inline_call in branch)
            or (branch.startswith("(let* ") and file_call in branch)
        )
    )


def consume_staged_request(wire):
    prefix = "anvil-mcp-staged-consumed:"
    if prefix not in expression:
        return wire
    match = re.search(r'base64-decode-string "([A-Za-z0-9+/=]+)"', expression)
    if match is None:
        raise SystemExit(70)
    path = Path(os.fsdecode(base64.b64decode(match.group(1), validate=True)))
    path.unlink()
    path.parent.rmdir()
    return prefix + wire


if "anvil-server-process-jsonrpc" in expression:
    if os.environ.get("FAKE_ATOMIC_NOT_READY") == "1":
        if not atomically_guards("anvil-server-process-jsonrpc"):
            bump("FAKE_DISPATCH_COUNT")
            Path(os.environ["FAKE_EARLY_DISPATCH"]).write_text(
                "atomic wrapper missing\n", encoding="utf-8"
            )
            print(f'"{os.environ["FAKE_RESPONSE_WIRE"]}"')
        else:
            Path(os.environ["FAKE_GUARD_OBSERVED"]).write_text(
                "jsonrpc\n", encoding="utf-8"
            )
            print('"anvil-mcp-headless-not-ready"')
        raise SystemExit(0)
    probes = int(Path(os.environ["FAKE_PROBE_COUNT"]).read_text())
    if probes <= int(os.environ.get("FAKE_NIL_BEFORE", "0")):
        Path(os.environ["FAKE_EARLY_DISPATCH"]).write_text(
            "dispatch before exact readiness\n", encoding="utf-8"
        )
        raise SystemExit(70)
    bump("FAKE_DISPATCH_COUNT")
    if os.environ.get("FAKE_SPLIT_SENTINEL") == "1":
        print('"anvil-mcp-headless-\nnot-ready"')
        raise SystemExit(0)
    if os.environ.get("FAKE_HANG_DISPATCH") == "1":
        if os.environ.get("FAKE_HANG_IGNORE_TERM") == "1":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(os.environ["FAKE_RUNNER_PID"]).write_text(
            str(os.getppid()), encoding="utf-8"
        )
        Path(os.environ["FAKE_HANG_PID"]).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        signal.pause()
    if os.environ.get("FAKE_DISPATCH_ERROR") == "1":
        raise SystemExit(70)
    if os.environ.get("FAKE_MALFORMED_OUTPUT") == "1":
        print('"')
    else:
        wire = consume_staged_request(os.environ["FAKE_RESPONSE_WIRE"])
        print(f'"{wire}"')
elif "(test-init)" in expression or "(test-stop)" in expression:
    kind = "init" if "(test-init)" in expression else "stop"
    count_name = "FAKE_INIT_COUNT" if kind == "init" else "FAKE_STOP_COUNT"
    not_ready = os.environ.get(f"FAKE_{kind.upper()}_NOT_READY") == "1"
    malformed = os.environ.get(f"FAKE_{kind.upper()}_MALFORMED") == "1"
    split = os.environ.get(f"FAKE_{kind.upper()}_SPLIT_SENTINEL") == "1"
    guarded = atomically_guards(f"(test-{kind})")
    if not_ready:
        if not guarded:
            bump(count_name)
            print('"anvil-mcp-lifecycle-complete"')
        else:
            Path(os.environ["FAKE_GUARD_OBSERVED"]).write_text(
                f"{kind}\n", encoding="utf-8"
            )
            print('"anvil-mcp-headless-not-ready"')
    elif malformed or split:
        if guarded:
            Path(os.environ["FAKE_GUARD_OBSERVED"]).write_text(
                f"{kind}\n", encoding="utf-8"
            )
        bump(count_name)
        if split:
            sys.stdout.write('"anvil-mcp-lifecycle-\ncomplete"\n')
        else:
            print('"')
    else:
        bump(count_name)
        print('"anvil-mcp-lifecycle-complete"')
elif "anvil-headless--ready-p" in expression:
    attempt = bump("FAKE_PROBE_COUNT")
    if os.environ.get("FAKE_INVALID_OUTPUT") == "1":
        output = "t-garbage"
    elif (
        os.environ.get("FAKE_ALWAYS_NIL") == "1"
        or attempt <= int(os.environ.get("FAKE_NIL_BEFORE", "0"))
    ):
        output = "nil"
    else:
        output = "t"
    ending = "\r\n" if os.environ.get("FAKE_CRLF") == "1" else "\n"
    sys.stdout.write(output + ending)
else:
    print(f"unexpected expression: {expression}", file=sys.stderr)
    raise SystemExit(64)
""".replace("__PYTHON__", sys.executable)
    make_executable(path, source)


def strict_equal(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            strict_equal(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            strict_equal(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def run_bridge_while_open(
    command: list[str],
    request: str,
    environment: dict[str, str],
    root: Path,
) -> tuple[str, str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        text=True,
        bufsize=1,
    )
    try:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise AssertionError("bridge pipes were not created")
        process.stdin.write(request)
        process.stdin.flush()

        executor = ThreadPoolExecutor(max_workers=1)
        reply_future = executor.submit(process.stdout.readline)
        try:
            try:
                first_line = reply_future.result(timeout=8)
            except FutureTimeoutError as error:
                process.kill()
                raise AssertionError("bridge did not return a bounded reply") from error
        finally:
            executor.shutdown(wait=True)

        if not first_line:
            raise AssertionError(
                f"bridge exited before replying with rc={process.poll()}"
            )
        if process.poll() is not None:
            raise AssertionError(
                f"bridge did not remain available after one request: "
                f"rc={process.returncode}"
            )
        staged = {
            path.name: (
                sorted(child.name for child in path.iterdir())
                if path.is_dir()
                else ["<not-a-directory>"]
            )
            for path in root.glob("anvil-mcp.*")
        }
        if staged:
            debug_log = root / "debug.log"
            debug = (
                debug_log.read_text(encoding="utf-8")
                if debug_log.exists()
                else "<no debug log>"
            )
            expression_file = root / "expression"
            expression = (
                expression_file.read_text(encoding="utf-8")
                if expression_file.exists()
                else "<no expression>"
            )
            guard_file = root / "guard-observed"
            early_file = root / "early-dispatch"
            guard = (
                guard_file.read_text(encoding="utf-8") if guard_file.exists() else ""
            )
            early = (
                early_file.read_text(encoding="utf-8") if early_file.exists() else ""
            )
            raise AssertionError(
                "bridge retained staged request custody after replying: "
                f"{staged!r}; response={first_line!r}; "
                f"guard={guard!r}; early={early!r}; "
                f"expression-head={expression[:240]!r}; "
                f"expression-tail={expression[-240:]!r}; debug={debug!r}"
            )

        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        process.stdin = None
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired as error:
            process.kill()
            raise AssertionError("bridge did not exit after stdin EOF") from error
        remainder = process.stdout.read()
        stderr = process.stderr.read()
        stdout = first_line + remainder
        if process.returncode != 0:
            raise AssertionError(
                f"bridge exited {process.returncode}: "
                f"stdout={stdout!r} stderr={stderr!r}"
            )
        return stdout, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def run_case(
    stdio: Path,
    bash: str,
    *,
    nil_before: int = 0,
    always_nil: bool = False,
    atomic_not_ready: bool = False,
    dispatch_error: bool = False,
    malformed_output: bool = False,
    split_sentinel: bool = False,
    invalid_output: bool = False,
    crlf: bool = False,
    readiness_timeout: int = 3,
    retry_delay_ms: int = 50,
    retry_max: int = 5,
    large_request: bool = False,
) -> tuple[dict[str, object], int, int, float, str, str]:
    with tempfile.TemporaryDirectory(prefix="anvil-stdio-readiness-") as raw:
        root = Path(raw)
        binary = root / "bin"
        binary.mkdir()
        fake = binary / "emacsclient"
        write_fake_emacsclient(fake)
        probe_count = root / "probe-count"
        dispatch_count = root / "dispatch-count"
        early_dispatch = root / "early-dispatch"
        guard_observed = root / "guard-observed"
        expected = {"jsonrpc": "2.0", "id": 17, "result": {"ready": True}}
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": 17,
            "method": "tools/call",
        }
        if large_request:
            request["params"] = {"payload": "x" * 20000}
        environment = os.environ.copy()
        environment.pop("ANVIL_MCP_PARENT_GUARD", None)
        environment.pop("ANVIL_MCP_PARENT_GUARD_PYTHON", None)
        environment.update(
            {
                "PATH": f"{binary}{os.pathsep}{environment['PATH']}",
                "TMPDIR": str(root),
                "EMACS_MCP_DEBUG_LOG": str(root / "debug.log"),
                "FAKE_PROBE_COUNT": str(probe_count),
                "FAKE_DISPATCH_COUNT": str(dispatch_count),
                "FAKE_EARLY_DISPATCH": str(early_dispatch),
                "FAKE_GUARD_OBSERVED": str(guard_observed),
                "FAKE_EXPRESSION": str(root / "expression"),
                "FAKE_INIT_COUNT": str(root / "init-count"),
                "FAKE_STOP_COUNT": str(root / "stop-count"),
                "FAKE_RESPONSE_WIRE": percent_wire(expected),
                "FAKE_NIL_BEFORE": str(nil_before),
                "FAKE_ALWAYS_NIL": "1" if always_nil else "0",
                "FAKE_ATOMIC_NOT_READY": "1" if atomic_not_ready else "0",
                "FAKE_DISPATCH_ERROR": "1" if dispatch_error else "0",
                "FAKE_MALFORMED_OUTPUT": "1" if malformed_output else "0",
                "FAKE_SPLIT_SENTINEL": "1" if split_sentinel else "0",
                "FAKE_HANG_DISPATCH": "0",
                "FAKE_HANG_IGNORE_TERM": "0",
                "FAKE_HANG_PID": str(root / "hang-pid"),
                "FAKE_RUNNER_PID": str(root / "runner-pid"),
                "FAKE_INVALID_OUTPUT": "1" if invalid_output else "0",
                "FAKE_CRLF": "1" if crlf else "0",
                "ANVIL_MCP_READINESS_MODE": "headless",
                "ANVIL_EMACSCLIENT_PROBE_TIMEOUT": "1",
                "ANVIL_EMACSCLIENT_READINESS_TIMEOUT": str(readiness_timeout),
                "ANVIL_EMACSCLIENT_STARTUP_DISPATCH_TIMEOUT": "2",
                "ANVIL_EMACSCLIENT_DISPATCH_TIMEOUT": "2",
                "ANVIL_EMACSCLIENT_KILL_AFTER_TIMEOUT": "1",
                "ANVIL_EMACSCLIENT_RETRY_MAX": str(retry_max),
                "ANVIL_EMACSCLIENT_RETRY_DELAY_MS": str(retry_delay_ms),
                "ANVIL_MCP_REQUEST_PARSE_TIMEOUT": "2",
                "ANVIL_MCP_FRAME_READ_TIMEOUT": "2",
            }
        )
        started = time.monotonic()
        stdout, stderr = run_bridge_while_open(
            [
                bash,
                str(stdio),
                "--socket=/tmp/anvil-readiness-test",
                "--server-id=anvil",
            ],
            json.dumps(request, separators=(",", ":")) + "\n",
            environment,
            root,
        )
        elapsed = time.monotonic() - started
        lines = [line for line in stdout.splitlines() if line]
        if len(lines) != 1:
            raise AssertionError(f"bridge returned {len(lines)} replies: {stdout!r}")
        response = json.loads(lines[0])
        if not isinstance(response, dict):
            raise AssertionError(f"bridge returned non-object: {response!r}")
        if early_dispatch.exists():
            raise AssertionError(early_dispatch.read_text(encoding="utf-8"))
        guard = (
            guard_observed.read_text(encoding="utf-8")
            if guard_observed.exists()
            else ""
        )
        debug_log = root / "debug.log"
        debug = debug_log.read_text(encoding="utf-8") if debug_log.exists() else ""
        diagnostics = stderr + ("\n" + debug if debug else "")
        return (
            response,
            read_count(probe_count),
            read_count(dispatch_count),
            elapsed,
            diagnostics,
            guard,
        )


def assert_readiness_error(response: dict[str, object], rc: int = 75) -> None:
    error = response.get("error")
    data = error.get("data") if isinstance(error, dict) else None
    expected = {
        "phase": "readiness",
        "dispatched": False,
        "replayed": False,
        "emacsclientRc": rc,
    }
    if response.get("id") != 17 or not strict_equal(data, expected):
        raise AssertionError(f"wrong readiness failure: {response!r}")


def assert_dispatch_error(response: dict[str, object], rc: int = 70) -> None:
    error = response.get("error")
    data = error.get("data") if isinstance(error, dict) else None
    expected = {
        "phase": "dispatch",
        "dispatched": True,
        "replayed": False,
        "emacsclientRc": rc,
    }
    if response.get("id") != 17 or not strict_equal(data, expected):
        raise AssertionError(f"wrong dispatch failure: {response!r}")


def assert_lifecycle_guard(
    stdio: Path, bash: str, kind: str, *, malformed: bool = False
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"anvil-stdio-{kind}-") as raw:
        root = Path(raw)
        binary = root / "bin"
        binary.mkdir()
        fake = binary / "emacsclient"
        write_fake_emacsclient(fake)
        probe_count = root / "probe-count"
        init_count = root / "init-count"
        stop_count = root / "stop-count"
        guard_observed = root / "guard-observed"
        environment = os.environ.copy()
        environment.pop("ANVIL_MCP_PARENT_GUARD", None)
        environment.pop("ANVIL_MCP_PARENT_GUARD_PYTHON", None)
        environment.update(
            {
                "PATH": f"{binary}{os.pathsep}{environment['PATH']}",
                "TMPDIR": str(root),
                "EMACS_MCP_DEBUG_LOG": str(root / "debug.log"),
                "FAKE_PROBE_COUNT": str(probe_count),
                "FAKE_DISPATCH_COUNT": str(root / "dispatch-count"),
                "FAKE_EARLY_DISPATCH": str(root / "early-dispatch"),
                "FAKE_GUARD_OBSERVED": str(guard_observed),
                "FAKE_INIT_COUNT": str(init_count),
                "FAKE_STOP_COUNT": str(stop_count),
                f"FAKE_{kind.upper()}_NOT_READY": "0" if malformed else "1",
                f"FAKE_{kind.upper()}_MALFORMED": "1" if malformed else "0",
                "ANVIL_MCP_READINESS_MODE": "headless",
                "ANVIL_EMACSCLIENT_PROBE_TIMEOUT": "1",
                "ANVIL_EMACSCLIENT_READINESS_TIMEOUT": "2",
                "ANVIL_EMACSCLIENT_STARTUP_DISPATCH_TIMEOUT": "2",
                "ANVIL_EMACSCLIENT_DISPATCH_TIMEOUT": "2",
                "ANVIL_EMACSCLIENT_KILL_AFTER_TIMEOUT": "1",
                "ANVIL_EMACSCLIENT_RETRY_MAX": "2",
                "ANVIL_EMACSCLIENT_RETRY_DELAY_MS": "0",
                "ANVIL_MCP_REQUEST_PARSE_TIMEOUT": "2",
                "ANVIL_MCP_FRAME_READ_TIMEOUT": "2",
            }
        )
        completed = subprocess.run(
            [
                bash,
                str(stdio),
                f"--{kind}-function=test-{kind}",
                "--socket=/tmp/anvil-readiness-test",
                "--server-id=anvil",
            ],
            input="",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            timeout=8,
            check=False,
        )
        count = read_count(init_count if kind == "init" else stop_count)
        guard = (
            guard_observed.read_text(encoding="utf-8")
            if guard_observed.exists()
            else ""
        )
        debug_log = root / "debug.log"
        debug = debug_log.read_text(encoding="utf-8") if debug_log.exists() else ""
        expected_count = 1 if malformed else 0
        expected_debug = f"MCP-{kind.upper()}-RC: 70"
        if (
            completed.returncode != 0
            or read_count(probe_count) != 1
            or count != expected_count
            or guard != f"{kind}\n"
            or "substring expression" in completed.stderr
            or (malformed and expected_debug not in debug)
        ):
            mode = "malformed" if malformed else "not-ready"
            raise AssertionError(
                f"{kind} {mode} lifecycle guard failed: "
                f"rc={completed.returncode} probes={read_count(probe_count)} "
                f"calls={count} guard={guard!r} stdout={completed.stdout!r} "
                f"stderr={completed.stderr!r} debug={debug!r}"
            )


def assert_signal_cleanup(stdio: Path, bash: str) -> None:
    if os.name != "posix":
        return

    with tempfile.TemporaryDirectory(prefix="anvil-stdio-signal-") as raw:
        root = Path(raw)
        binary = root / "bin"
        binary.mkdir()
        fake = binary / "emacsclient"
        write_fake_emacsclient(fake)
        hang_pid_path = root / "hang-pid"
        runner_pid_path = root / "runner-pid"
        request = {
            "jsonrpc": "2.0",
            "id": 29,
            "method": "tools/call",
            "params": {"payload": "x" * 20000},
        }
        environment = os.environ.copy()
        environment.pop("ANVIL_MCP_PARENT_GUARD", None)
        environment.pop("ANVIL_MCP_PARENT_GUARD_PYTHON", None)
        environment.update(
            {
                "PATH": f"{binary}{os.pathsep}{environment['PATH']}",
                "TMPDIR": str(root),
                "EMACS_MCP_DEBUG_LOG": str(root / "debug.log"),
                "FAKE_PROBE_COUNT": str(root / "probe-count"),
                "FAKE_DISPATCH_COUNT": str(root / "dispatch-count"),
                "FAKE_EARLY_DISPATCH": str(root / "early-dispatch"),
                "FAKE_GUARD_OBSERVED": str(root / "guard-observed"),
                "FAKE_EXPRESSION": str(root / "expression"),
                "FAKE_INIT_COUNT": str(root / "init-count"),
                "FAKE_STOP_COUNT": str(root / "stop-count"),
                "FAKE_RESPONSE_WIRE": percent_wire(
                    {"jsonrpc": "2.0", "id": 29, "result": True}
                ),
                "FAKE_NIL_BEFORE": "0",
                "FAKE_ALWAYS_NIL": "0",
                "FAKE_ATOMIC_NOT_READY": "0",
                "FAKE_DISPATCH_ERROR": "0",
                "FAKE_MALFORMED_OUTPUT": "0",
                "FAKE_HANG_DISPATCH": "1",
                "FAKE_HANG_IGNORE_TERM": "1",
                "FAKE_HANG_PID": str(hang_pid_path),
                "FAKE_RUNNER_PID": str(runner_pid_path),
                "FAKE_INVALID_OUTPUT": "0",
                "FAKE_CRLF": "0",
                "ANVIL_MCP_READINESS_MODE": "headless",
                "ANVIL_EMACSCLIENT_PROBE_TIMEOUT": "1",
                "ANVIL_EMACSCLIENT_READINESS_TIMEOUT": "2",
                "ANVIL_EMACSCLIENT_STARTUP_DISPATCH_TIMEOUT": "10",
                "ANVIL_EMACSCLIENT_DISPATCH_TIMEOUT": "10",
                "ANVIL_EMACSCLIENT_KILL_AFTER_TIMEOUT": "1",
                "ANVIL_EMACSCLIENT_RETRY_MAX": "2",
                "ANVIL_EMACSCLIENT_RETRY_DELAY_MS": "0",
                "ANVIL_MCP_REQUEST_PARSE_TIMEOUT": "2",
                "ANVIL_MCP_FRAME_READ_TIMEOUT": "2",
            }
        )
        process = subprocess.Popen(
            [
                bash,
                str(stdio),
                "--socket=/tmp/anvil-readiness-test",
                "--server-id=anvil",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            start_new_session=True,
        )
        fake_pid: int | None = None
        runner_pid: int | None = None
        try:
            if (
                process.stdin is None
                or process.stdout is None
                or process.stderr is None
            ):
                raise AssertionError("signal regression pipes were not created")
            process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            process.stdin.flush()

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                if (
                    hang_pid_path.exists()
                    and runner_pid_path.exists()
                    and list(root.glob("anvil-mcp.*"))
                ):
                    fake_pid = int(hang_pid_path.read_text(encoding="utf-8"))
                    runner_pid = int(runner_pid_path.read_text(encoding="utf-8"))
                    break
                time.sleep(0.02)
            if fake_pid is None or runner_pid is None:
                raise AssertionError(
                    "large request did not reach its staged hanging dispatch"
                )

            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise AssertionError(
                    "direct bridge SIGTERM did not produce a bounded exit"
                ) from error

            stdout = process.stdout.read()
            stderr = process.stderr.read()
            deadline = time.monotonic() + 2
            child_alive = True
            runner_alive = True
            staged = list(root.glob("anvil-mcp.*"))
            while time.monotonic() < deadline:
                try:
                    os.kill(fake_pid, 0)
                except ProcessLookupError:
                    child_alive = False
                except PermissionError:
                    child_alive = True
                else:
                    child_alive = True
                try:
                    os.kill(runner_pid, 0)
                except ProcessLookupError:
                    runner_alive = False
                except PermissionError:
                    runner_alive = True
                else:
                    runner_alive = True
                staged = list(root.glob("anvil-mcp.*"))
                if not child_alive and not runner_alive and not staged:
                    break
                time.sleep(0.02)
            if (
                process.returncode != 143
                or child_alive
                or runner_alive
                or staged
                or stdout
                or "substring expression" in stderr
            ):
                raise AssertionError(
                    "direct bridge SIGTERM failed custody: "
                    f"rc={process.returncode} child_alive={child_alive} "
                    f"runner_alive={runner_alive} "
                    f"staged={[path.name for path in staged]!r} "
                    f"stdout={stdout!r} stderr={stderr!r}"
                )
        finally:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            if fake_pid is not None:
                try:
                    os.kill(fake_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if runner_pid is not None:
                try:
                    os.kill(runner_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def assert_launch_signal_cleanup(stdio: Path, bash: str) -> None:
    """A signal in the runner-publication window must not orphan it."""
    if os.name != "posix":
        return

    with tempfile.TemporaryDirectory(prefix="anvil-stdio-launch-signal-") as raw:
        root = Path(raw)
        binary = root / "bin"
        binary.mkdir()
        write_fake_emacsclient(binary / "emacsclient")
        marker = root / "launch-marker"
        release = root / "launch-release"

        source = stdio.read_text(encoding="utf-8")
        child_needle = (
            'anvil_mcp_run_child() {\n'
            '\tlocal guard_deadline="$1" stderr_mode="$2" '
            'input_mode="$3" input="$4"\n'
            '\tlocal child="" rc=70 runner_pid="" timed_out=0\n'
            '\tshift 4\n'
        )
        child_replacement = child_needle + (
            '\tif [ -n "${ANVIL_MCP_TEST_HANG_RUNNER:-}" ]; then\n'
            '\t\twhile :; do :; done\n'
            '\tfi\n'
        )
        launch_needle = (
            '\trunner=$!\n'
            '\tANVIL_MCP_ACTIVE_RUNNER=$runner\n'
        )
        launch_replacement = (
            '\trunner=$!\n'
            '\tif [ -n "${ANVIL_MCP_TEST_LAUNCH_MARKER:-}" ]; then\n'
            '\t\tprintf \'%s\\n\' "$runner" > '
            '"$ANVIL_MCP_TEST_LAUNCH_MARKER"\n'
            '\t\twhile [ ! -e "$ANVIL_MCP_TEST_LAUNCH_RELEASE" ]; do :; done\n'
            '\tfi\n'
            '\tANVIL_MCP_ACTIVE_RUNNER=$runner\n'
        )
        if source.count(child_needle) != 1 or source.count(launch_needle) != 1:
            raise AssertionError("bounded runner launch markers changed")
        patched = source.replace(child_needle, child_replacement, 1).replace(
            launch_needle, launch_replacement, 1
        )
        bridge = root / "anvil-stdio-launch-signal.sh"
        bridge.write_text(patched, encoding="utf-8")
        bridge.chmod(0o755)

        environment = os.environ.copy()
        environment.pop("ANVIL_MCP_PARENT_GUARD", None)
        environment.pop("ANVIL_MCP_PARENT_GUARD_PYTHON", None)
        environment.update(
            {
                "PATH": f"{binary}{os.pathsep}{environment['PATH']}",
                "TMPDIR": str(root),
                "ANVIL_MCP_TEST_HANG_RUNNER": "1",
                "ANVIL_MCP_TEST_LAUNCH_MARKER": str(marker),
                "ANVIL_MCP_TEST_LAUNCH_RELEASE": str(release),
                "ANVIL_EMACSCLIENT_KILL_AFTER_TIMEOUT": "1",
                "ANVIL_MCP_REQUEST_PARSE_TIMEOUT": "2",
                "ANVIL_MCP_FRAME_READ_TIMEOUT": "2",
            }
        )
        process = subprocess.Popen(
            [
                bash,
                str(bridge),
                "--socket=/tmp/anvil-launch-signal-test",
                "--server-id=anvil",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            start_new_session=True,
        )
        runner_pid: int | None = None
        try:
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise AssertionError("launch-signal regression pipes were not created")
            process.stdin.write("{}\n")
            process.stdin.flush()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if marker.exists():
                    runner_pid = int(marker.read_text(encoding="utf-8"))
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            if runner_pid is None:
                raise AssertionError("bridge did not enter the runner launch window")

            process.terminate()
            release.touch()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise AssertionError("launch-window SIGTERM did not exit") from error

            deadline = time.monotonic() + 2
            runner_alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(runner_pid, 0)
                except ProcessLookupError:
                    runner_alive = False
                    break
                except PermissionError:
                    pass
                time.sleep(0.02)
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            if process.returncode != 143 or runner_alive or stdout:
                raise AssertionError(
                    "launch-window signal lost runner custody: "
                    f"rc={process.returncode} runner_alive={runner_alive} "
                    f"stdout={stdout!r} stderr={stderr!r}"
                )
        finally:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            if runner_pid is not None:
                try:
                    os.kill(runner_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def assert_output_cap_recovers(stdio: Path, bash: str) -> None:
    """A helper-output overflow is bounded and the same bridge recovers."""
    with tempfile.TemporaryDirectory(prefix="anvil-stdio-output-cap-") as raw:
        root = Path(raw)
        source = stdio.read_text(encoding="utf-8")
        needle = "readonly ANVIL_MCP_MAX_HELPER_OUTPUT_BYTES=67108864"
        if source.count(needle) != 1:
            raise AssertionError("helper output cap marker changed")
        bridge = root / "anvil-stdio.sh"
        bridge.write_text(
            source.replace(
                needle,
                "readonly ANVIL_MCP_MAX_HELPER_OUTPUT_BYTES=1024",
            ),
            encoding="utf-8",
        )
        bridge.chmod(0o755)
        binary = root / "bin"
        binary.mkdir()
        fake = binary / "emacsclient"
        make_executable(
            fake,
            r"""#!__PYTHON__
import os
from pathlib import Path
import sys

expression = sys.argv[-1]
if "anvil-server-process-jsonrpc" not in expression:
    print("t")
    raise SystemExit(0)
count_path = Path(os.environ["FAKE_DISPATCH_COUNT"])
try:
    count = int(count_path.read_text(encoding="utf-8")) + 1
except FileNotFoundError:
    count = 1
count_path.write_text(str(count), encoding="utf-8")
if count == 1:
    sys.stdout.write('"' + ('x' * 2048) + '"\n')
else:
    print('"' + os.environ["FAKE_RESPONSE_WIRE"] + '"')
""".replace("__PYTHON__", sys.executable),
        )
        expected = {"jsonrpc": "2.0", "id": 32, "result": "recovered"}
        environment = os.environ.copy()
        environment.pop("ANVIL_MCP_PARENT_GUARD", None)
        environment.pop("ANVIL_MCP_PARENT_GUARD_PYTHON", None)
        environment.update(
            {
                "PATH": f"{binary}{os.pathsep}{environment['PATH']}",
                "TMPDIR": str(root),
                "FAKE_DISPATCH_COUNT": str(root / "dispatch-count"),
                "FAKE_RESPONSE_WIRE": percent_wire(expected),
                "ANVIL_MCP_READINESS_MODE": "headless",
                "ANVIL_EMACSCLIENT_PROBE_TIMEOUT": "1",
                "ANVIL_EMACSCLIENT_READINESS_TIMEOUT": "2",
                "ANVIL_EMACSCLIENT_STARTUP_DISPATCH_TIMEOUT": "3",
                "ANVIL_EMACSCLIENT_DISPATCH_TIMEOUT": "3",
                "ANVIL_EMACSCLIENT_KILL_AFTER_TIMEOUT": "1",
                "ANVIL_EMACSCLIENT_RETRY_MAX": "2",
                "ANVIL_EMACSCLIENT_RETRY_DELAY_MS": "0",
                "ANVIL_MCP_REQUEST_PARSE_TIMEOUT": "2",
                "ANVIL_MCP_FRAME_READ_TIMEOUT": "2",
            }
        )
        process = subprocess.Popen(
            [
                bash,
                str(bridge),
                "--socket=/tmp/anvil-output-cap-test",
                "--server-id=anvil",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            bufsize=1,
        )
        try:
            if process.stdin is None or process.stdout is None:
                raise AssertionError("output cap bridge pipes were not created")

            def exchange(request_id: int) -> dict[str, object]:
                process.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "tools/call",
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                process.stdin.flush()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(process.stdout.readline)
                    try:
                        line = future.result(timeout=8)
                    except FutureTimeoutError as error:
                        raise AssertionError("bounded output returned no reply") from error
                if not line:
                    raise AssertionError(
                        f"output cap bridge exited early: {process.poll()}"
                    )
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise AssertionError(f"invalid output cap reply: {value!r}")
                return value

            overflow = exchange(31)
            data = (
                overflow.get("error", {}).get("data")
                if isinstance(overflow.get("error"), dict)
                else None
            )
            if not strict_equal(
                data,
                {
                    "phase": "dispatch",
                    "dispatched": True,
                    "replayed": False,
                    "emacsclientRc": 70,
                },
            ):
                raise AssertionError(f"wrong output-cap failure: {overflow!r}")
            recovered = exchange(32)
            if not strict_equal(recovered, expected):
                raise AssertionError(f"output-cap recovery failed: {recovered!r}")
            if read_count(root / "dispatch-count") != 2:
                raise AssertionError("output-cap request was replayed")
            process.stdin.close()
            process.stdin = None
            process.wait(timeout=5)
            if process.returncode != 0:
                stderr = process.stderr.read() if process.stderr is not None else ""
                raise AssertionError(f"output-cap bridge exited {process.returncode}: {stderr}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


def assert_invalid_configuration(
    stdio: Path,
    bash: str,
    name: str,
    value: str,
    *,
    expected_fragment: str | None = None,
    server_id: str = "anvil",
) -> None:
    with tempfile.TemporaryDirectory(prefix="anvil-stdio-config-") as raw:
        binary = Path(raw) / "bin"
        binary.mkdir()
        make_executable(binary / "emacsclient", f"#!{bash}\nexit 0\n")
        environment = os.environ.copy()
        environment.pop("ANVIL_MCP_PARENT_GUARD", None)
        environment.pop("ANVIL_MCP_PARENT_GUARD_PYTHON", None)
        environment.update(
            {
                "PATH": f"{binary}{os.pathsep}{environment['PATH']}",
                "ANVIL_MCP_READINESS_MODE": "emacs",
                "ANVIL_EMACSCLIENT_PROBE_TIMEOUT": "1",
                "ANVIL_EMACSCLIENT_READINESS_TIMEOUT": "2",
                "ANVIL_EMACSCLIENT_STARTUP_DISPATCH_TIMEOUT": "2",
                "ANVIL_EMACSCLIENT_DISPATCH_TIMEOUT": "2",
                "ANVIL_EMACSCLIENT_KILL_AFTER_TIMEOUT": "1",
                "ANVIL_EMACSCLIENT_RETRY_MAX": "2",
                "ANVIL_EMACSCLIENT_RETRY_DELAY_MS": "0",
                "ANVIL_MCP_REQUEST_PARSE_TIMEOUT": "2",
                "ANVIL_MCP_FRAME_READ_TIMEOUT": "2",
                name: value,
            }
        )
        completed = subprocess.run(
            [bash, str(stdio), f"--server-id={server_id}"],
            input="",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            timeout=3,
            check=False,
        )
        fragment = expected_fragment or name
        if completed.returncode != 64 or fragment not in completed.stderr:
            raise AssertionError(
                f"invalid {name}={value!r} was not rejected cleanly: "
                f"rc={completed.returncode} stderr={completed.stderr!r}"
            )


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} ANVIL_STDIO BASH")
    stdio = Path(sys.argv[1]).resolve()
    bash = str(Path(sys.argv[2]).resolve())
    expected = {"jsonrpc": "2.0", "id": 17, "result": {"ready": True}}

    success, probes, dispatches, _elapsed, stderr, _guard = run_case(
        stdio, bash, nil_before=2
    )
    if not strict_equal(success, expected) or probes != 3 or dispatches != 1:
        raise AssertionError(
            f"nil-to-ready case failed: response={success!r} "
            f"probes={probes} dispatches={dispatches} stderr={stderr!r}"
        )

    crlf, probes, dispatches, _elapsed, _stderr, _guard = run_case(
        stdio, bash, crlf=True
    )
    if not strict_equal(crlf, expected) or probes != 1 or dispatches != 1:
        raise AssertionError(
            f"CRLF readiness was not accepted: response={crlf!r} "
            f"probes={probes} dispatches={dispatches}"
        )

    exhausted, probes, dispatches, elapsed, _stderr, _guard = run_case(
        stdio, bash, always_nil=True, readiness_timeout=1
    )
    assert_readiness_error(exhausted)
    if probes < 1 or dispatches != 0 or not 0 < elapsed < 2.5:
        raise AssertionError(
            "nil deadline was not bounded and fail-closed: "
            f"probes={probes} dispatches={dispatches} elapsed={elapsed:.3f}"
        )

    delayed, probes, dispatches, elapsed, _stderr, _guard = run_case(
        stdio,
        bash,
        always_nil=True,
        readiness_timeout=1,
        retry_delay_ms=5000,
    )
    assert_readiness_error(delayed)
    if probes < 1 or dispatches != 0 or not 0 < elapsed < 2.5:
        raise AssertionError(
            "retry delay exceeded the readiness budget: "
            f"probes={probes} dispatches={dispatches} elapsed={elapsed:.3f}"
        )

    raced, probes, dispatches, _elapsed, _stderr, guard = run_case(
        stdio, bash, atomic_not_ready=True
    )
    assert_readiness_error(raced)
    if probes != 1 or dispatches != 0 or guard != "jsonrpc\n":
        raise AssertionError(
            "atomic JSON-RPC guard was not observed before dispatch: "
            f"probes={probes} dispatches={dispatches} guard={guard!r}"
        )

    large_raced, probes, dispatches, _elapsed, _stderr, guard = run_case(
        stdio, bash, atomic_not_ready=True, large_request=True
    )
    assert_readiness_error(large_raced)
    if probes != 1 or dispatches != 0 or guard != "jsonrpc\n":
        raise AssertionError(
            "large-request atomic guard was not observed: "
            f"probes={probes} dispatches={dispatches} guard={guard!r}"
        )

    failed, probes, dispatches, _elapsed, _stderr, _guard = run_case(
        stdio, bash, dispatch_error=True, large_request=True
    )
    assert_dispatch_error(failed)
    if probes != 1 or dispatches != 1:
        raise AssertionError(
            "large request dispatch failure was not singular: "
            f"probes={probes} dispatches={dispatches}"
        )

    malformed, probes, dispatches, _elapsed, stderr, _guard = run_case(
        stdio, bash, malformed_output=True
    )
    assert_dispatch_error(malformed)
    if probes != 1 or dispatches != 1 or "substring expression" in stderr:
        raise AssertionError(
            "one-character dispatch output did not fail closed: "
            f"probes={probes} dispatches={dispatches} stderr={stderr!r}"
        )

    split, probes, dispatches, _elapsed, _stderr, _guard = run_case(
        stdio, bash, split_sentinel=True
    )
    assert_dispatch_error(split)
    if probes != 1 or dispatches != 1:
        raise AssertionError(
            "split sentinel was misclassified as replay-safe: "
            f"probes={probes} dispatches={dispatches}"
        )

    invalid, probes, dispatches, _elapsed, _stderr, _guard = run_case(
        stdio, bash, invalid_output=True
    )
    assert_readiness_error(invalid)
    if probes != 1 or dispatches != 0:
        raise AssertionError(
            f"invalid probe output was accepted: probes={probes} "
            f"dispatches={dispatches}"
        )

    assert_lifecycle_guard(stdio, bash, "init")
    assert_lifecycle_guard(stdio, bash, "stop")
    assert_lifecycle_guard(stdio, bash, "init", malformed=True)
    assert_lifecycle_guard(stdio, bash, "stop", malformed=True)
    assert_signal_cleanup(stdio, bash)
    assert_launch_signal_cleanup(stdio, bash)
    assert_output_cap_recovers(stdio, bash)
    assert_invalid_configuration(stdio, bash, "ANVIL_EMACSCLIENT_RETRY_MAX", "09")
    assert_invalid_configuration(
        stdio, bash, "ANVIL_EMACSCLIENT_READINESS_TIMEOUT", "09"
    )
    assert_invalid_configuration(stdio, bash, "ANVIL_EMACSCLIENT_RETRY_MAX", "9" * 200)
    assert_invalid_configuration(
        stdio,
        bash,
        "ANVIL_EMACSCLIENT_READINESS_TIMEOUT",
        "9" * 200,
    )
    assert_invalid_configuration(
        stdio,
        bash,
        "ANVIL_MCP_READINESS_MODE",
        "unsupported",
        expected_fragment="unsupported readiness mode",
    )
    assert_invalid_configuration(
        stdio,
        bash,
        "ANVIL_MCP_READINESS_MODE",
        "headless",
        expected_fragment="unsafe server id",
        server_id="unsafe id",
    )

    print(f"stdio-readiness-ok bash={bash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
