#!/usr/bin/env python3
"""
DotRush LSP stdio proxy (man-in-the-middle) for the `dotrush` Claude Code plugin.

    Claude Code  <->  this proxy  <->  DotRush language server

- Forwards the client<->server byte streams verbatim, frame-by-frame.
- Injects custom LSP messages into the client->server direction at frame
  boundaries (from a FIFO), without desyncing JSON-RPC request/response pairing.
- Auto-installs the DotRush server on first run if it is missing.

Control channel (newline-delimited JSON, one JSON-RPC message per line):
    echo '{"method":"dotrush/solutionDiagnostics","params":{}}' > "$DOTRUSH_INJECT_FIFO"

Env (set by the plugin's .lsp.json; all optional with sensible fallbacks):
    DOTRUSH_REAL_BIN        explicit path to the DotRush executable (overrides discovery)
    DOTRUSH_SERVER_DIR      dir the server lives in / is installed to
    DOTRUSH_INSTALL_SCRIPT  installer to run if the server is missing
    DOTRUSH_INJECT_FIFO     control FIFO path
    DOTRUSH_PROXY_LOG       log file path (empty string disables logging)
"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = "DotRush.exe" if os.name == "nt" else "DotRush"

SERVER_DIR = os.environ.get("DOTRUSH_SERVER_DIR") or os.path.join(HERE, "..", "server")
REAL_BIN = os.environ.get("DOTRUSH_REAL_BIN") or os.path.join(SERVER_DIR, EXE)
INSTALL_SCRIPT = os.environ.get("DOTRUSH_INSTALL_SCRIPT") or os.path.join(HERE, "..", "scripts", "install-dotrush.sh")
FIFO_PATH = os.environ.get("DOTRUSH_INJECT_FIFO") or os.path.join(SERVER_DIR, "..", "inject.fifo")
LOG_PATH = os.environ.get("DOTRUSH_PROXY_LOG", os.path.join(SERVER_DIR, "..", "proxy.log"))
# Persisted project/solution choice (the roslyn config section) — written by the
# `dotrush-setup` skill, replayed here at startup so the target loads without a
# dotrush.config.json in the user's repo.
TARGET_FILE = os.environ.get("DOTRUSH_TARGET_FILE") or os.path.join(SERVER_DIR, "..", "target.json")

_log_lock = threading.Lock()
_stdin_lock = threading.Lock()  # serializes writes into DotRush's stdin


def log(msg):
    if not LOG_PATH:
        return
    line = f"{time.strftime('%H:%M:%S')} [{os.getpid()}] {msg}\n"
    with _log_lock:
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            with open(LOG_PATH, "a") as f:
                f.write(line)
        except OSError:
            pass


def frame(body_bytes):
    return f"Content-Length: {len(body_bytes)}\r\n\r\n".encode("ascii") + body_bytes


class FrameReader:
    """Reads LSP frames from a raw fd, preserving exact header+body bytes."""

    def __init__(self, fd):
        self.fd = fd
        self.buf = b""

    def _fill(self):
        chunk = os.read(self.fd, 65536)
        if not chunk:
            return False
        self.buf += chunk
        return True

    def read_frame(self):
        while b"\r\n\r\n" not in self.buf:
            if not self._fill():
                return None
        head, rest = self.buf.split(b"\r\n\r\n", 1)
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        self.buf = rest
        while len(self.buf) < length:
            if not self._fill():
                return None
        body = self.buf[:length]
        self.buf = self.buf[length:]
        return head + b"\r\n\r\n", body


def brief(body):
    try:
        m = json.loads(body)
    except Exception:
        return f"<unparseable {len(body)}b>"
    if "method" in m and "id" in m:
        return f"request  id={m['id']} {m['method']}"
    if "method" in m:
        return f"notif    {m['method']}"
    if "id" in m:
        return f"response id={m['id']}"
    return "<unknown>"


def pump_client_to_server(child_stdin):
    reader = FrameReader(0)
    while True:
        f = reader.read_frame()
        if f is None:
            log("client stdin EOF -> closing server stdin")
            try:
                child_stdin.close()
            except OSError:
                pass
            return
        header, body = f
        with _stdin_lock:
            child_stdin.write(header + body)
            child_stdin.flush()


def pump_server_to_client(child_stdout):
    reader = FrameReader(child_stdout.fileno())
    while True:
        f = reader.read_frame()
        if f is None:
            log("server stdout EOF")
            return
        header, body = f
        os.write(1, header + body)
        b = brief(body)
        if not b.startswith("response"):
            log(f"S->C {b}")


def pump_stderr(child_stderr):
    while True:
        chunk = child_stderr.read(65536)
        if not chunk:
            return
        os.write(2, chunk)


def injector(child_stdin):
    try:
        os.makedirs(os.path.dirname(FIFO_PATH), exist_ok=True)
        if not os.path.exists(FIFO_PATH):
            os.mkfifo(FIFO_PATH)
    except OSError as e:
        log(f"cannot create FIFO {FIFO_PATH}: {e}")
        return
    log(f"injector watching FIFO {FIFO_PATH}")
    while True:
        try:
            with open(FIFO_PATH, "r") as fifo:
                for raw in fifo:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError as e:
                        log(f"INJECT skipped (bad JSON): {e}: {line[:120]}")
                        continue
                    if not isinstance(msg, dict) or "method" not in msg:
                        log(f"INJECT skipped (needs JSON object with 'method'): {line[:120]}")
                        continue
                    msg.setdefault("jsonrpc", "2.0")
                    body = json.dumps(msg).encode("utf-8")
                    with _stdin_lock:
                        try:
                            child_stdin.write(frame(body))
                            child_stdin.flush()
                        except (OSError, ValueError) as e:
                            log(f"INJECT write failed (server gone?): {e}")
                            return
                    log(f"INJECT -> {brief(body)}")
        except OSError as e:
            log(f"FIFO reopen after error: {e}")
            time.sleep(0.5)


def ensure_server():
    """Return a path to the DotRush executable, auto-installing it if missing."""
    if os.path.exists(REAL_BIN):
        return REAL_BIN
    if os.path.exists(INSTALL_SCRIPT):
        log(f"DotRush server missing at {REAL_BIN}; running installer {INSTALL_SCRIPT}")
        sys.stderr.write("dotrush: server not found, downloading it (first run, ~one time)...\n")
        try:
            # capture installer output so it never leaks into the LSP stdout stream
            res = subprocess.run(
                ["bash", INSTALL_SCRIPT, SERVER_DIR],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for ln in (res.stdout or "").splitlines():
                log(f"installer: {ln}")
            if res.returncode != 0:
                log(f"installer exited {res.returncode}")
                sys.stderr.write(f"dotrush: install failed (exit {res.returncode}); see {LOG_PATH}\n")
        except Exception as e:  # noqa: BLE001
            log(f"installer error: {e}")
            sys.stderr.write(f"dotrush: install error: {e}\n")
    else:
        log(f"no installer at {INSTALL_SCRIPT}")
    return REAL_BIN if os.path.exists(REAL_BIN) else None


def startup_config_inject(child_stdin):
    """Replay the persisted target as workspace/didChangeConfiguration, first thing.

    DotRush's initialize awaits configuration before loading projects, so pushing
    this ahead of the client's messages makes the chosen solution load at startup —
    no dotrush.config.json required. No-op if nothing has been chosen yet.
    """
    try:
        if not os.path.exists(TARGET_FILE):
            return
        with open(TARGET_FILE) as f:
            roslyn = json.load(f)
        if not isinstance(roslyn, dict) or not roslyn.get("projectOrSolutionFiles"):
            log(f"target file has no projectOrSolutionFiles: {TARGET_FILE}")
            return
    except (OSError, json.JSONDecodeError) as e:
        log(f"target file unreadable ({TARGET_FILE}): {e}")
        return
    msg = {"jsonrpc": "2.0", "method": "workspace/didChangeConfiguration",
           "params": {"settings": {"dotrush": {"roslyn": roslyn}}}}
    body = json.dumps(msg).encode("utf-8")
    with _stdin_lock:
        try:
            child_stdin.write(frame(body))
            child_stdin.flush()
            log(f"startup: applied persisted target {roslyn.get('projectOrSolutionFiles')}")
        except (OSError, ValueError) as e:
            log(f"startup config inject failed: {e}")


def main():
    real = ensure_server()
    if not real:
        sys.stderr.write(
            "dotrush: DotRush server is not available and could not be installed.\n"
            f"  Expected at: {REAL_BIN}\n"
            f"  Install manually: bash '{INSTALL_SCRIPT}' '{SERVER_DIR}'\n"
        )
        sys.exit(127)

    log(f"proxy start: exec {real}")
    child = subprocess.Popen(
        [real] + sys.argv[1:],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    )
    startup_config_inject(child.stdin)  # push the persisted target before the client's traffic
    for t in (
        threading.Thread(target=pump_client_to_server, args=(child.stdin,), daemon=True),
        threading.Thread(target=pump_stderr, args=(child.stderr,), daemon=True),
        threading.Thread(target=injector, args=(child.stdin,), daemon=True),
    ):
        t.start()
    try:
        pump_server_to_client(child.stdout)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
        log(f"proxy exit (server rc={child.poll()})")


if __name__ == "__main__":
    main()
