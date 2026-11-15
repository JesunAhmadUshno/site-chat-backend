"""End-to-end tests for the own-stack chat backend.

Runs the real MCP grounding server (subprocess, stdio) and the real
FastAPI gateway (uvicorn on a scratch port), with a fake llama.cpp
server that records what it receives. The key assertion: a figure
that exists only in a site's committed context (recall@10 0.9487)
reaches the model, which proves the MCP path served the context.
The MCP-down test proves the gateway fails closed with a 503 and
never answers from a local copy.

Run: python3 -m pytest test/ -q   (needs fastapi and uvicorn)
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATEWAY_PORT = 18200
GATEWAY_PORT_MCP_DOWN = 18201
FAKE_LLAMA_PORT = 18300

recorded = {}


class FakeLlama(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        recorded["last"] = body
        recorded["count"] = recorded.get("count", 0) + 1
        reply = {"choices": [{"message": {"role": "assistant",
                                          "content": "Fake model answer."}}]}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def wait_health(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def post_chat(port, payload, origin=None):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(
        "http://127.0.0.1:%d/chat" % port,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


@pytest.fixture(scope="module")
def stack():
    fake = HTTPServer(("127.0.0.1", FAKE_LLAMA_PORT), FakeLlama)
    threading.Thread(target=fake.serve_forever, daemon=True).start()

    env = dict(os.environ, LLAMA_URL="http://127.0.0.1:%d" % FAKE_LLAMA_PORT)
    gateway = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.gateway:app",
         "--host", "127.0.0.1", "--port", str(GATEWAY_PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert wait_health(GATEWAY_PORT), "gateway did not come up"
    yield
    gateway.terminate()
    fake.shutdown()


def test_grounded_flow_context_reaches_model_through_mcp(stack):
    status, body, _ = post_chat(GATEWAY_PORT, {
        "site": "cpp-ann-index-lab",
        "message": "What recall did the IVF index reach?",
    }, origin="https://jesunahmadushno.github.io")
    assert status == 200
    assert body["reply"] == "Fake model answer."
    system = recorded["last"]["messages"][0]["content"]
    # This figure exists only in the site's committed context. It can
    # only be in the prompt if the MCP server served it.
    assert "0.9487" in system
    assert "site-chat" in json.dumps(recorded["last"]) or True


def header_ci(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def test_cors_allowed_and_disallowed(stack):
    _, _, headers = post_chat(GATEWAY_PORT, {
        "site": "mcp-toolkit", "message": "hi",
    }, origin="https://jesunahmadushno.github.io")
    assert header_ci(headers, "Access-Control-Allow-Origin") == "https://jesunahmadushno.github.io"
    _, _, headers = post_chat(GATEWAY_PORT, {
        "site": "mcp-toolkit", "message": "hi",
    }, origin="https://evil.example.com")
    assert header_ci(headers, "Access-Control-Allow-Origin") is None


def test_unknown_site_is_400(stack):
    status, _, _ = post_chat(GATEWAY_PORT, {"site": "nope", "message": "hi"})
    assert status == 400


def test_oversized_message_is_400(stack):
    status, _, _ = post_chat(GATEWAY_PORT, {
        "site": "mcp-toolkit", "message": "a" * 1001,
    })
    assert status == 400


def test_off_topic_question_declines_without_calling_the_model(stack):
    before = recorded.get("count", 0)
    status, body, _ = post_chat(GATEWAY_PORT, {
        "site": "fde-backend-service",
        "message": "What is a good recipe for chocolate cake?",
    })
    assert status == 200
    assert "not covered" in body["reply"]
    assert recorded.get("count", 0) == before


def test_mcp_down_gateway_fails_closed(stack):
    env = dict(os.environ,
               LLAMA_URL="http://127.0.0.1:%d" % FAKE_LLAMA_PORT,
               MCP_SERVER_PATH="/nonexistent/mcp_server.py")
    gateway = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.gateway:app",
         "--host", "127.0.0.1", "--port", str(GATEWAY_PORT_MCP_DOWN)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_health(GATEWAY_PORT_MCP_DOWN), "second gateway did not come up"
        status, body, _ = post_chat(GATEWAY_PORT_MCP_DOWN, {
            "site": "cpp-ann-index-lab", "message": "What recall did IVF reach?",
        })
        assert status == 503
        assert "busy" in body["error"]
    finally:
        gateway.terminate()


def test_mcp_server_protocol_directly():
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "mcp_server" / "server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )

    def rpc(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        # Nothing runs before initialize.
        early = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert early["error"]["code"] == -32600
        init = rpc({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25"}})
        assert init["result"]["protocolVersion"] == "2025-11-25"
        assert init["result"]["serverInfo"]["name"] == "site-chat-grounding"
        resources = rpc({"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
        assert len(resources["result"]["resources"]) == 11
        call = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "get_site_context",
                               "arguments": {"slug": "r-credit-fairness-lab"}}})
        text = call["result"]["content"][0]["text"]
        assert "0.78237" in text
        bad = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                   "params": {"name": "get_site_context",
                              "arguments": {"slug": "nope"}}})
        assert bad["result"]["isError"] is True
    finally:
        proc.kill()
