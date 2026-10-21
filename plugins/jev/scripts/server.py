#!/usr/bin/env python3
"""Minimal stdio MCP server exposing the embedded Jev decision engine to Codex.

The engine is embedded in ``../jev_engine`` (the upstream decision logic with
its Hermes registration shim removed), so the plugin depends on no Hermes
installation. Tool definitions and dispatch share one table, so each tool is
declared once.
"""
from __future__ import annotations

import importlib
import json
import warnings
import sys
from pathlib import Path
from typing import Any

# The adapter SDK emits third-party Pydantic serializer warnings on stderr on
# every call. They are noise for an MCP server: stdout carries the protocol.
warnings.filterwarnings("ignore", message=r"Pydantic serializer warnings")

SERVER_NAME = "jev"
SERVER_VERSION = "1.3.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# MCP tool name -> (schema attribute, handler attribute) on the engine.
TOOLS = {
    "jev": ("JEV_SCHEMA", "_handle_jev"),
    "jev_review": ("JEV_REVIEW_SCHEMA", "_handle_jev_review"),
    "jev_guard": ("JEV_GUARD_SCHEMA", "_handle_jev_guard"),
}

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def load_engine() -> Any:
    """Import the vendored engine package from this plugin's directory."""
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    return importlib.import_module("jev_engine")


def tool_definitions(engine: Any) -> list[dict[str, Any]]:
    definitions = []
    for name, (schema_attr, _) in TOOLS.items():
        schema = getattr(engine, schema_attr)
        definitions.append(
            {
                "name": name,
                "description": schema["description"],
                "inputSchema": schema["parameters"],
            }
        )
    return definitions


def call_tool(engine: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        _, handler_attr = TOOLS[name]
    except KeyError:
        raise KeyError(f"Unknown tool: {name}") from None
    payload = getattr(engine, handler_attr)(arguments)
    try:
        data = json.loads(payload)
    except Exception:
        data = payload
    result = {
        "content": [{"type": "text", "text": payload}],
        "structuredContent": data if isinstance(data, dict) else {"result": data},
    }
    if isinstance(data, dict) and "error" in data:
        result["isError"] = True
    return result


def error_result(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps({"error": message}, ensure_ascii=False)}],
        "structuredContent": {"error": message},
        "isError": True,
    }


def handle(request: dict[str, Any], engine: Any) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Call Jev only when several execution paths are genuinely plausible, "
                    "or when a risky action needs a structured second opinion. "
                    "An error is not an allow verdict."
                ),
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_definitions(engine)}}
    if method == "tools/call":
        try:
            result = call_tool(engine, params.get("name"), params.get("arguments") or {})
        except Exception as exc:
            result = error_result(f"Jev adapter failed: {type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    try:
        engine = load_engine()
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        sys.stderr.flush()
        return 1

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Request must be a JSON object")
            response = handle(request, engine)
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if response is not None:
            send(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
