"""A small persistent MCP client for the site-chat grounding server.

Speaks newline-delimited JSON-RPC 2.0 over the server's stdio, the
transport the gateway and the server share on one host. One subprocess
is held for the life of the gateway, guarded by a lock. If the server
process dies, the client restarts it once per call; if the call still
fails, the client raises McpUnavailable and the gateway answers with
its busy message. There is deliberately no fallback copy of the
contexts in the gateway: no MCP, no answers.
"""

import json
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVER = REPO_ROOT / "mcp_server" / "server.py"
PROTOCOL_VERSION = "2025-11-25"


class McpUnavailable(Exception):
    pass


class UnknownSite(Exception):
    pass


class McpClient:
    def __init__(self, server_path=None, python=None):
        self.server_path = str(server_path or DEFAULT_SERVER)
        self.python = python or sys.executable
        self._proc = None
        self._next_id = 0
        self._lock = threading.Lock()

    # -- process management -------------------------------------------------
    def _start(self):
        self._stop()
        self._proc = subprocess.Popen(
            [self.python, self.server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "site-chat-gateway", "version": "1.0.0"},
        })
        self._notify("notifications/initialized", {})

    def _stop(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    # -- JSON-RPC over stdio --------------------------------------------------
    def _send(self, payload):
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _request(self, method, params):
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        line = self._proc.stdout.readline()
        if not line:
            raise McpUnavailable("MCP server closed the stream")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpUnavailable("MCP server sent invalid JSON") from exc
        if response.get("id") != request_id:
            raise McpUnavailable("MCP server response id mismatch")
        if "error" in response:
            raise McpUnavailable("MCP error: " + str(response["error"].get("message")))
        return response.get("result")

    def _notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def ping(self):
        """Start the server if needed and answer True if it responds."""
        with self._lock:
            try:
                if not self.alive():
                    self._start()
                self._request("ping", {})
                return True
            except Exception:
                self._stop()
                return False

    # -- public API -----------------------------------------------------------
    def get_site_context(self, slug):
        """Return the grounded context text for one site, via tools/call."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if not self.alive():
                        self._start()
                    result = self._request("tools/call", {
                        "name": "get_site_context",
                        "arguments": {"slug": slug},
                    })
                    break
                except Exception as exc:
                    self._stop()
                    if attempt == 2:
                        raise McpUnavailable(str(exc)) from exc
            content = (result or {}).get("content") or []
            text = content[0].get("text") if content and isinstance(content[0], dict) else None
            if (result or {}).get("isError") or text is None:
                raise UnknownSite(slug)
            return text
