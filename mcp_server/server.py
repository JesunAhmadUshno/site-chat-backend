#!/usr/bin/env python3
"""site-chat grounding server: an MCP server in the mcp-toolkit pattern.

Standard library only. Speaks MCP over stdio (newline-delimited
JSON-RPC 2.0), negotiates protocol versions 2025-11-25 down to
2024-11-05, enforces the lifecycle (nothing runs before initialize),
and uses the standard JSON-RPC error codes.

It serves exactly one kind of thing: the grounded context of Jesun's
portfolio sites, loaded from contexts/contexts.json. Each site is:

- a resource: sitechat://context/<slug> (resources/list, resources/read)
- the return value of the get_site_context tool (tools/list, tools/call)

The chat gateway holds no context of its own. If this server is down,
the gateway cannot answer, by design.

Usage: python3 mcp_server/server.py [path-to-contexts.json]
"""

import json
import sys
from pathlib import Path

PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "site-chat-grounding", "version": "1.0.0"}


def load_contexts(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not data:
        raise ValueError("contexts file must be a non-empty JSON object")
    for slug, ctx in data.items():
        for field in ("name", "what", "results", "ceiling"):
            if field not in ctx:
                raise ValueError("context %s is missing field %s" % (slug, field))
    return data


def render_context(slug, ctx):
    lines = [
        "Project: " + ctx["name"],
        "What it is: " + ctx["what"],
        "Measured results:",
    ]
    lines.extend("- " + r for r in ctx["results"])
    lines.append("Claims ceiling: " + ctx["ceiling"])
    return "\n".join(lines)


class GroundingServer:
    def __init__(self, contexts):
        self.contexts = contexts
        self.initialized = False

    # -- JSON-RPC plumbing -------------------------------------------------
    def handle(self, msg):
        """Return a response dict, or None for notifications."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "invalid_request")
        method = msg.get("method")
        msg_id = msg.get("id")
        is_notification = msg_id is None

        if method == "initialize":
            params = msg.get("params") or {}
            requested = params.get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            self.initialized = True
            return self._result(msg_id, {
                "protocolVersion": version,
                "capabilities": {"resources": {}, "tools": {}},
                "serverInfo": SERVER_INFO,
            })
        if is_notification:
            return None
        if not self.initialized:
            return self._error(msg_id, -32600, "not_initialized")

        handler = {
            "ping": self._ping,
            "resources/list": self._resources_list,
            "resources/read": self._resources_read,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
        }.get(method)
        if handler is None:
            return self._error(msg_id, -32601, "method_not_found")
        try:
            return handler(msg_id, msg.get("params") or {})
        except Exception:
            return self._error(msg_id, -32603, "internal_error")

    def _result(self, msg_id, result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _error(self, msg_id, code, reason):
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": reason},
        }

    # -- Methods ------------------------------------------------------------
    def _ping(self, msg_id, params):
        return self._result(msg_id, {})

    def _resources_list(self, msg_id, params):
        resources = [
            {
                "uri": "sitechat://context/" + slug,
                "name": ctx["name"] + " context",
                "description": "Grounded context for the " + slug + " site.",
                "mimeType": "application/json",
            }
            for slug, ctx in sorted(self.contexts.items())
        ]
        return self._result(msg_id, {"resources": resources})

    def _resources_read(self, msg_id, params):
        uri = params.get("uri", "")
        slug = uri.rsplit("/", 1)[-1] if uri.startswith("sitechat://context/") else None
        if slug not in self.contexts:
            return self._error(msg_id, -32002, "resource_not_found")
        return self._result(msg_id, {
            "contents": [{
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(self.contexts[slug], indent=2),
            }]
        })

    def _tools_list(self, msg_id, params):
        return self._result(msg_id, {"tools": [{
            "name": "get_site_context",
            "description": "Return the grounded context text for one portfolio site, by slug.",
            "inputSchema": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        }]})

    def _tools_call(self, msg_id, params):
        if params.get("name") != "get_site_context":
            return self._error(msg_id, -32602, "unknown_tool")
        args = params.get("arguments") or {}
        slug = args.get("slug")
        if slug not in self.contexts:
            return self._result(msg_id, {
                "content": [{"type": "text", "text": "Unknown site: " + str(slug)}],
                "isError": True,
            })
        return self._result(msg_id, {
            "content": [{"type": "text", "text": render_context(slug, self.contexts[slug])}],
            "isError": False,
        })


def main(argv):
    default = Path(__file__).resolve().parent.parent / "contexts" / "contexts.json"
    path = Path(argv[1]) if len(argv) > 1 else default
    server = GroundingServer(load_contexts(path))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "parse_error"}}
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue
        response = server.handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
