"""Synthetic Anthropic-compatible provider + MCP endpoint for CLI contract probes.

Runs ONLY on an internal-only Docker network, never with real credentials.
It records what the official CLI sends (paths, header *names*, advertised tool
names) so the contract tests can assert the exact tool surface. It never logs
header values or message text, so the log is safe to keep as evidence.

Environment:
    FAKE_TOOL_CALL  optional "<tool_name>|<json input>": on the first request
                    whose last message is not a tool_result, the fake model
                    asks the CLI to call that tool, then answers "DONE".
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_TOOL_CALL = os.environ.get("FAKE_TOOL_CALL", "")
_AUTH_HEADERS = ("x-api-key", "authorization")


def _log(event: dict) -> None:
    sys.stdout.write("PROBE " + json.dumps(event, sort_keys=True) + "\n")
    sys.stdout.flush()


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _last_is_tool_result(messages: list) -> bool:
    if not messages:
        return False
    content = messages[-1].get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _tool_names(body: dict) -> list[str]:
    return sorted(
        t.get("name", "") for t in body.get("tools", []) if isinstance(t, dict)
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:  # silence default stderr log
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("content-length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        _log({"kind": "http", "method": "GET", "path": self.path})
        self._send(404, b"{}")

    def do_POST(self) -> None:
        body = self._read_json()
        if self.path.startswith("/mcp"):
            self._mcp(body)
            return
        if self.path.startswith("/v1/messages/count_tokens"):
            _log({"kind": "http", "method": "POST", "path": self.path})
            self._send(200, json.dumps({"input_tokens": 1}).encode())
            return
        if not self.path.startswith("/v1/messages"):
            _log({"kind": "http", "method": "POST", "path": self.path})
            self._send(404, b"{}")
            return
        self._messages(body)

    def _messages(self, body: dict) -> None:
        headers = {h: (h in {k.lower() for k in self.headers}) for h in _AUTH_HEADERS}
        _log(
            {
                "kind": "messages",
                "path": self.path,
                "auth_header_present": headers,
                "model": body.get("model"),
                "stream": bool(body.get("stream")),
                "tools": _tool_names(body),
            }
        )
        call = None
        if _TOOL_CALL and not _last_is_tool_result(body.get("messages", [])):
            name, _, raw_input = _TOOL_CALL.partition("|")
            call = (name, raw_input or "{}")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        model = body.get("model", "fake-model")
        chunks = [
            _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_probe",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                },
            )
        ]
        if call:
            chunks += [
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "toolu_probe",
                            "name": call[0],
                            "input": {},
                        },
                    },
                ),
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": call[1]},
                    },
                ),
            ]
            stop = "tool_use"
        else:
            chunks += [
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "DONE"},
                    },
                ),
            ]
            stop = "end_turn"
        chunks += [
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        ]
        for chunk in chunks:
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _mcp(self, body: dict) -> None:
        method = body.get("method")
        rpc_id = body.get("id")
        _log({"kind": "mcp", "method": method, "path": self.path})
        if rpc_id is None:  # notification
            self._send(202, b"")
            return
        if method == "initialize":
            result = {
                "protocolVersion": (body.get("params") or {}).get(
                    "protocolVersion", "2025-03-26"
                ),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "probe", "version": "0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo a message (contract probe)",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = body.get("params") or {}
            _log(
                {"kind": "mcp_tool_call", "name": params.get("name"), "path": self.path}
            )
            result = {"content": [{"type": "text", "text": "echoed"}], "isError": False}
        else:
            result = {}
        self._send(
            200, json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result}).encode()
        )


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)  # nosec B104 - internal-only probe network
    _log({"kind": "ready"})
    server.serve_forever()
