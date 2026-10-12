"""Explicit one-case synthetic consent, never a production human approval host."""

from __future__ import annotations

from dataclasses import dataclass

from tests.codex_native_mcp_server import SERVER


@dataclass
class SyntheticMcpConsent:
    thread_id: str
    nonce: str
    turn_id: str | None = None
    consumed: bool = False

    def answer(self, request: dict, events: list[dict]) -> dict | None:
        if self.consumed:
            return {"action": "decline"}
        if self.turn_id is None:
            return None  # The real turn/start response must establish identity first.
        self.consumed = True
        params = request.get("params")
        if not isinstance(params, dict):
            return {"action": "decline"}
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            return {"action": "decline"}
        matches = []
        terminal = False
        for event in events:
            bound = event.get("params", {})
            item = bound.get("item") if isinstance(bound, dict) else None
            if isinstance(bound, dict) and bound.get("threadId") == self.thread_id:
                if (
                    event.get("method") == "turn/completed"
                    and isinstance(bound.get("turn"), dict)
                    and bound["turn"].get("id") == self.turn_id
                ):
                    terminal = True
                if (
                    event.get("method") == "item/completed"
                    and bound.get("turnId") == self.turn_id
                    and isinstance(item, dict)
                    and item.get("type") == "mcpToolCall"
                    and item.get("server") == SERVER
                    and item.get("tool") == "probe"
                    and item.get("arguments") == {"nonce": self.nonce}
                ):
                    terminal = True
            if (
                event.get("method") == "item/started"
                and isinstance(item, dict)
                and bound.get("threadId") == self.thread_id
                and bound.get("turnId") == self.turn_id
                and item.get("type") == "mcpToolCall"
                and item.get("server") == SERVER
                and item.get("tool") == "probe"
                and item.get("arguments") == {"nonce": self.nonce}
                and item.get("status") == "inProgress"
            ):
                matches.append(item)
        allowed = (
            request.get("method") == "mcpServer/elicitation/request"
            and params.get("threadId") == self.thread_id
            and params.get("turnId") == self.turn_id
            and params.get("serverName") == SERVER
            and params.get("mode") == "form"
            and params.get("requestedSchema") == {"type": "object", "properties": {}}
            and meta.get("codex_approval_kind") == "mcp_tool_call"
            and meta.get("tool_params") == {"nonce": self.nonce}
            and len(matches) == 1
            and not terminal
        )
        return {"action": "accept", "content": {}} if allowed else {"action": "decline"}
