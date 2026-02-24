#!/usr/bin/env python3
"""Parse a Task agent output file and report token usage + cost estimate."""
import json
import sys

# Pricing per 1M tokens (USD)
PRICING = {
    "claude-opus-4-6": {"input": 15, "output": 75, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6": {"input": 3, "output": 15, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 0.80, "output": 4, "cache_write": 1.00, "cache_read": 0.08},
}


def analyze(path: str) -> dict:
    input_tokens = 0
    output_tokens = 0
    cache_create = 0
    cache_read = 0
    models: dict[str, int] = {}
    requests = 0
    tool_uses = 0

    with open(path) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            msg = obj.get("message", {})
            if msg.get("role") != "assistant":
                continue

            # Count tool uses
            for content in msg.get("content", []):
                if isinstance(content, dict) and content.get("type") == "tool_use":
                    tool_uses += 1

            usage = msg.get("usage", {})
            if not usage:
                continue
            model = msg.get("model", "unknown")
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)
            cache_create += usage.get("cache_creation_input_tokens", 0)
            cache_read += usage.get("cache_read_input_tokens", 0)
            models[model] = models.get(model, 0) + 1
            requests += 1

    # Calculate cost
    total_cost = 0
    cost_detail = {}
    for model, count in models.items():
        p = PRICING.get(model, PRICING["claude-opus-4-6"])
        # Proportional split by request count (simplified)
        ratio = count / requests if requests else 0
        c = {
            "input": (input_tokens * ratio / 1e6) * p["input"],
            "output": (output_tokens * ratio / 1e6) * p["output"],
            "cache_write": (cache_create * ratio / 1e6) * p["cache_write"],
            "cache_read": (cache_read * ratio / 1e6) * p["cache_read"],
        }
        c["subtotal"] = sum(c.values())
        cost_detail[model] = c
        total_cost += c["subtotal"]

    return {
        "requests": requests,
        "tool_uses": tool_uses,
        "models": models,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_create": cache_create,
        "cache_read": cache_read,
        "total_tokens": input_tokens + output_tokens + cache_create + cache_read,
        "cost_detail": cost_detail,
        "total_cost": total_cost,
    }


def format_report(data: dict) -> str:
    lines = ["=== Task Cost Report ==="]
    lines.append(f"API Requests: {data['requests']} | Tool Uses: {data['tool_uses']}")

    model_parts = []
    for m, c in data["models"].items():
        pct = c / data["requests"] * 100 if data["requests"] else 0
        model_parts.append(f"{m}: {c} ({pct:.0f}%)")
    lines.append(f"Models: {', '.join(model_parts)}")
    lines.append("")
    lines.append("Token Breakdown:")
    lines.append(f"  Input:        {data['input_tokens']:>12,}")
    lines.append(f"  Output:       {data['output_tokens']:>12,}")
    lines.append(f"  Cache write:  {data['cache_create']:>12,}")
    lines.append(f"  Cache read:   {data['cache_read']:>12,}")
    lines.append(f"  Total:        {data['total_tokens']:>12,}")
    lines.append("")
    lines.append("Cost Estimate:")
    for model, c in data["cost_detail"].items():
        lines.append(f"  [{model}]")
        lines.append(f"    Input:       ${c['input']:.4f}")
        lines.append(f"    Output:      ${c['output']:.4f}")
        lines.append(f"    Cache write: ${c['cache_write']:.4f}")
        lines.append(f"    Cache read:  ${c['cache_read']:.4f}")
        lines.append(f"    Subtotal:    ${c['subtotal']:.4f}")
    lines.append(f"  TOTAL:         ${data['total_cost']:.4f}")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <task_output_file>")
        sys.exit(1)
    data = analyze(sys.argv[1])
    print(format_report(data))
