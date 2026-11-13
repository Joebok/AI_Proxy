from __future__ import annotations

import json
import time
from urllib.request import urlopen


def extract_metrics(answer: dict, request: dict) -> dict:
    result = {key: answer.get(key) for key in ("total_duration_ns", "load_duration_ns", "prompt_eval_duration_ns", "eval_duration_ns", "prompt_eval_count", "prompt_eval_cached_count", "eval_count", "done_reason")}
    mapping = {"total_duration": "total_duration_ns", "load_duration": "load_duration_ns", "prompt_eval_duration": "prompt_eval_duration_ns", "eval_duration": "eval_duration_ns", "prompt_eval_count": "prompt_eval_count", "prompt_eval_cached_count": "prompt_eval_cached_count", "eval_count": "eval_count"}
    for source, target in mapping.items():
        if source in answer:
            result[target] = answer[source]
    usage = answer.get("usage")
    if isinstance(usage, dict):
        for source, target in (("prompt_tokens", "prompt_eval_count"), ("completion_tokens", "eval_count")):
            if source in usage:
                result[target] = usage[source]
    count, duration = result.get("eval_count"), result.get("eval_duration_ns")
    if isinstance(count, (int, float)) and isinstance(duration, (int, float)) and duration > 0:
        result["tokens_per_second"] = count * 1_000_000_000 / duration
    options = request.get("options", {})
    for name in ("num_ctx", "num_predict", "think", "reasoning_effort"):
        value = options.get(name, request.get(name))
        if isinstance(value, (int, float, bool, str)):
            result[name] = value
    return result


def parse_stream_metrics(sample: bytes) -> dict:
    """Read bounded Ollama JSON, NDJSON or SSE records without retaining text."""
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    records = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    if not records:
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                records.append(value)
        except json.JSONDecodeError:
            return {}
    merged = {}
    for record in records:
        merged.update({k: v for k, v in record.items() if k in {"model", "done_reason", "total_duration", "load_duration", "prompt_eval_duration", "eval_duration", "prompt_eval_count", "prompt_eval_cached_count", "eval_count", "usage"}})
    return merged


def sample_residency(upstream: str, requested_model: str, effective_model: str | None = None) -> dict:
    try:
        started = time.monotonic()
        with urlopen(upstream.rstrip("/") + "/api/ps", timeout=0.2) as response:
            if time.monotonic() - started > 0.25:
                raise TimeoutError()
            models = json.load(response).get("models", [])
        names = [effective_model, requested_model]
        for name in names:
            if not name:
                continue
            matches = [m for m in models if m.get("name", m.get("model")) == name]
            if not matches and name.endswith(":latest"):
                matches = [m for m in models if m.get("name", m.get("model")) == name[:-7]]
            if len(matches) == 1:
                model = matches[0]
                size, vram = model.get("size"), model.get("size_vram")
                result = {"model_digest": model.get("digest"), "quantization": model.get("details", {}).get("quantization_level"), "context_length": model.get("context_length"), "size_bytes": size, "size_vram_bytes": vram, "residency_status": "sampled"}
                if isinstance(size, (int, float)) and size > 0 and isinstance(vram, (int, float)) and 0 <= vram <= size:
                    result.update(gpu_percent=100 * vram / size, cpu_percent=100 * (size-vram) / size)
                return result
            if len(matches) > 1:
                return {"residency_status": "ambiguous"}
        return {"residency_status": "not_loaded"}
    except Exception:
        return {"residency_status": "probe_failed"}
