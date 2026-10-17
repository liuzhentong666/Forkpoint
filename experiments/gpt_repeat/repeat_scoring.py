"""Deterministic grading of measured events; no model self-grading."""
import json
import math
from pathlib import Path


def peer_overwrites(before, after, actor, owners):
    """Detect every published regression, even if a later commit restores it."""
    events = []
    for resource, (owner, protected_value) in owners.items():
        if owner != actor and after.get(resource) != protected_value:
            events.append({"actor": actor, "owner": owner, "resource": resource,
                           "expected": protected_value, "actual": after.get(resource)})
    for resource in before.keys() | after.keys():
        if before.get(resource) != after.get(resource):
            owners[resource] = (actor, after.get(resource))
    return events


def trace_metrics(directory, thread_ids):
    directory = Path(directory)
    totals, routes, models, requests, replies = {}, [], {}, {}, {}
    for raw in (directory / "server.jsonl").read_bytes().splitlines():
        event = json.loads(raw)
        method, p = event.get("method"), event.get("params", {})
        if method == "thread/tokenUsage/updated":
            totals[p["threadId"]] = p["tokenUsage"]["total"]
        elif method == "model/rerouted":
            routes.append(p)
        elif method == "item/tool/call":
            requests[event["id"]] = p
        elif isinstance(event.get("result"), dict) and "thread" in event["result"] and "model" in event["result"]:
            r = event["result"]
            models[r["thread"]["id"]] = {key: r.get(key) for key in ("model", "modelProvider", "reasoningEffort", "serviceTier")}
    for raw in (directory / "tool_calls.jsonl").read_bytes().splitlines():
        event = json.loads(raw)
        if "result" in event:
            replies[event["id"]] = event["result"]
    missing_usage = [tid for tid in thread_ids if tid not in totals]
    keys = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")
    summed = {key: sum(totals[t].get(key, 0) for t in thread_ids) for key in keys} if not missing_usage else None
    return {"model_by_thread": models, "model_reroutes": routes,
            "tool_calls": len(requests), "tool_responses": len(replies),
            "tool_calls_by_thread": {tid: sum(p["threadId"] == tid for p in requests.values()) for tid in thread_ids},
            "unanswered_tool_request_ids": sorted(set(requests)-set(replies), key=str),
            "tokens_by_thread": {tid: totals.get(tid) for tid in thread_ids},
            "tokens_total": summed, "missing_token_threads": missing_usage}


def wilson(successes, attempts):
    if not attempts:
        return None
    z, p = 1.959963984540054, successes / attempts
    divisor = 1 + z*z/attempts
    center = (p + z*z/(2*attempts)) / divisor
    radius = z*math.sqrt(p*(1-p)/attempts + z*z/(4*attempts*attempts)) / divisor
    return [max(0, center-radius), min(1, center+radius)]
