"""Deterministic metrics from raw Claude Code stream-json and MCP logs; no model self-grading."""
import json
from pathlib import Path

TOKEN_KEYS = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")


def read_jsonl(path):
    path = Path(path)
    if not path.is_file():
        return []
    events = []
    for line in path.read_bytes().splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except ValueError:
                events.append({"type": "unparsed", "raw": line.decode("utf-8", "replace")})
    return events


def model_usage_tokens(model_usage):
    """Cumulative usage of one Claude Code process, from a result event's modelUsage.

    Assistant events in the stream carry usage snapshots taken while a response is still
    streaming, so only result events are used. inputTokens there is uncached input only.
    """
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    rows = list(model_usage.values())
    uncached = sum(r.get("inputTokens", 0) for r in rows)
    read = sum(r.get("cacheReadInputTokens", 0) for r in rows)
    write = sum(r.get("cacheCreationInputTokens", 0) for r in rows)
    output = sum(r.get("outputTokens", 0) for r in rows)
    thinking = sum(r["thinkingTokens"] for r in rows) if all("thinkingTokens" in r for r in rows) else None
    return {"inputTokens": uncached + read + write, "cachedInputTokens": read, "cacheWriteInputTokens": write,
            "outputTokens": output, "reasoningOutputTokens": thinking, "totalTokens": uncached + read + write + output}


def agent_trace(directory, actor):
    directory = Path(directory)
    events = read_jsonl(directory / f"{actor}.stdout.jsonl")
    prompts = sum(e.get("type") == "user" for e in read_jsonl(directory / f"{actor}.stdin.jsonl"))
    inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    init = inits[0] if inits else {}
    responses, tool_uses, tool_results = {}, {}, set()
    for event in events:
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        if event.get("type") == "assistant" and event.get("parent_tool_use_id") is None:
            responses[message.get("id")] = message.get("model")
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_uses[block.get("id")] = block.get("name")
        elif event.get("type") == "user" and isinstance(message.get("content"), list):
            tool_results.update(b.get("tool_use_id") for b in message["content"]
                                if isinstance(b, dict) and b.get("type") == "tool_result")
    results = [e for e in events if e.get("type") == "result"]
    limits = [e.get("rate_limit_info") or {} for e in events if e.get("type") == "rate_limit_event"]
    mcp_requests, mcp_replies = {}, {}
    for line in read_jsonl(directory / f"tool_calls.{actor}.jsonl"):
        if line.get("method") == "tools/call":
            mcp_requests[line.get("id")] = line.get("params") or {}
        elif "id" in line:
            mcp_replies[line.get("id")] = line
    mcp_tool_use_ids = [(p.get("_meta") or {}).get("claudecode/toolUseId") for p in mcp_requests.values()]
    observed = model_usage_tokens(results[-1].get("modelUsage")) if results else None
    complete = bool(prompts) and len(results) == prompts
    return {"init": {k: init.get(k) for k in ("session_id", "model", "claude_code_version", "tools",
                                              "mcp_servers", "permissionMode", "apiKeySource")},
            "init_models": sorted({str(e.get("model")) for e in inits}),
            "prompts": prompts, "turn_results": [{k: r.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms",
                                                                         "total_cost_usd", "result")} for r in results],
            "api_responses": len(responses),
            "response_models": sorted({str(m) for m in responses.values()}),
            "stream_tool_uses": len(tool_uses), "stream_tool_names": sorted(set(map(str, tool_uses.values()))),
            "stream_unanswered_tool_uses": sorted(set(tool_uses) - tool_results, key=str),
            "mcp_tool_requests": len(mcp_requests), "mcp_tool_responses": len(mcp_replies),
            "mcp_unanswered_ids": sorted(set(mcp_requests) - set(mcp_replies), key=str),
            "mcp_calls_without_stream_tool_use": [i for i in mcp_tool_use_ids if i not in tool_uses],
            "usage_complete": complete, "tokens": observed if complete else None, "observed_tokens": observed,
            "model_usage": results[-1].get("modelUsage") if results else None,
            "list_cost_usd": results[-1].get("total_cost_usd") if results else None,
            "rate_limit_last": limits[-1] if limits else None,
            "rate_limit_not_allowed": [x for x in limits if x.get("status") != "allowed"]}


def add_tokens(rows):
    rows = [r for r in rows if r is not None]
    if not rows:
        return None
    total = {k: sum(r[k] for r in rows) for k in TOKEN_KEYS if k != "reasoningOutputTokens"}
    total["reasoningOutputTokens"] = (sum(r["reasoningOutputTokens"] for r in rows)
                                      if all(r["reasoningOutputTokens"] is not None for r in rows) else None)
    return total


def trace_metrics(directory, actors, requested_model):
    agents = {actor: agent_trace(directory, actor) for actor in actors}
    reroutes = [{"actor": a, "models": t["response_models"]} for a, t in agents.items()
                if any(m != requested_model for m in t["response_models"])]
    init_mismatches = [{"actor": a, "models": t["init_models"]} for a, t in agents.items()
                       if any(m != requested_model for m in t["init_models"])]
    missing = [a for a, t in agents.items() if t["tokens"] is None]
    return {"model_by_agent": {a: {"init_model": t["init"]["model"], "response_models": t["response_models"],
                                   "claude_code_version": t["init"]["claude_code_version"]}
                               for a, t in agents.items() if t["init"]["model"] or t["response_models"]},
            "model_reroutes": reroutes, "init_mismatches": init_mismatches,
            "tool_calls": sum(t["mcp_tool_requests"] for t in agents.values()),
            "tool_responses": sum(t["mcp_tool_responses"] for t in agents.values()),
            "tool_calls_by_agent": {a: t["mcp_tool_requests"] for a, t in agents.items()},
            "stream_tool_uses_by_agent": {a: t["stream_tool_uses"] for a, t in agents.items()},
            "unanswered_tool_request_ids": [f"{a}:{i}" for a, t in agents.items() for i in t["mcp_unanswered_ids"]]
                                           + [f"{a}:{i}" for a, t in agents.items() for i in t["stream_unanswered_tool_uses"]],
            "mcp_calls_without_stream_tool_use": {a: t["mcp_calls_without_stream_tool_use"] for a, t in agents.items()
                                                  if t["mcp_calls_without_stream_tool_use"]},
            "api_responses_by_agent": {a: t["api_responses"] for a, t in agents.items()},
            "tokens_by_agent": {a: t["tokens"] for a, t in agents.items()},
            "tokens_total": None if missing else add_tokens(t["tokens"] for t in agents.values()),
            "observed_tokens_total": add_tokens(t["observed_tokens"] for t in agents.values()),
            "missing_token_agents": missing,
            "list_cost_usd": round(sum(t["list_cost_usd"] or 0 for t in agents.values()), 6),
            "rate_limit_not_allowed": {a: t["rate_limit_not_allowed"] for a, t in agents.items() if t["rate_limit_not_allowed"]},
            "agents": agents}
