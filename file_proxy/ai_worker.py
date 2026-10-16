"""One immutable AI request, executed by a trusted provider registration."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import Request, urlopen
from uuid import uuid4

from .models import iso_utc, utc_now
from .validation import validate_relative_reference


def write_json(path: Path, value) -> None:
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def local_file(root: Path, name: str) -> Path:
    # Reuse the envelope path contract; neither prompts nor outputs choose host paths.
    validate_relative_reference(root, name)
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("AI artifact escapes its job")
    return path


def http_json(url: str, value=None, timeout=60):
    data = json.dumps(value).encode() if value is not None else None
    req = Request(url, data=data, headers={"Content-Type": "application/json"},
                  method="POST" if data is not None else "GET")
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def validate_structure(value, schema, location="answer"):
    """Validate the structural JSON-schema contract used by provider requests."""
    if schema is True:
        return
    if schema is False:
        raise ValueError(f"{location} is forbidden by its schema")
    for choice in ("anyOf", "oneOf"):
        if choice in schema:
            valid = 0
            for alternative in schema[choice]:
                try:
                    validate_structure(value, alternative, location)
                    valid += 1
                except ValueError:
                    pass
            if not valid or (choice == "oneOf" and valid != 1):
                raise ValueError(f"{location} does not satisfy {choice}")
    types = {"object": dict, "array": list, "string": str, "boolean": bool,
             "integer": int, "number": (int, float), "null": type(None)}
    requested = schema.get("type")
    if requested:
        accepted = requested if isinstance(requested, list) else [requested]
        if not any(isinstance(value, types[kind]) and not (kind in {"number", "integer"} and isinstance(value, bool)) for kind in accepted):
            raise ValueError(f"{location} has the wrong JSON type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{location} is outside its allowed values")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value):
            raise ValueError(f"{location} is missing required fields")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError(f"{location} contains unexpected fields")
        for key, item in value.items():
            if key in properties:
                validate_structure(item, properties[key], location + "." + key)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise ValueError(f"{location} has an invalid item count")
        if isinstance(schema.get("items"), (dict, bool)):
            for index, item in enumerate(value):
                validate_structure(item, schema["items"], f"{location}[{index}]")


def codex_executable() -> str:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        paths = list((Path(os.environ["LOCALAPPDATA"]) / "OpenAI/Codex/bin").glob("*/codex.exe"))
        if paths:
            return str(max(paths, key=lambda path: path.stat().st_mtime_ns))
    path = shutil.which("codex")
    if not path:
        raise RuntimeError("Codex CLI is unavailable on the AI_Proxy machine")
    return path


def run_codex(root, request, output, executable=None):
    prefix = executable if isinstance(executable, list) else [executable or codex_executable()]
    command = [*prefix, "-a", "never", "-s", "read-only", "-m",
               request["model"].removeprefix("codex:"), "-c",
               'model_reasoning_effort=' + json.dumps(request.get("options", {}).get("effort", "high")),
               "-C", str(root.resolve()), "exec", "--ignore-user-config", "--skip-git-repo-check",
               "--ephemeral", "--output-last-message", str(output.resolve())]
    if request.get("schema"):
        schema_path = root / ".response-schema.json"
        write_json(schema_path, request["schema"])
        command += ["--output-schema", str(schema_path.resolve())]
    for item in request.get("images", []):
        command += ["--image", str(local_file(root, item).resolve())]
    completed = subprocess.run(command, input=request.get("system", "") + "\n\n" + request["prompt"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=float(request.get("options", {}).get("timeout_seconds", 1800)),
                               check=False, shell=False)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "Codex failed")[-4000:])
    if not output.is_file():
        raise ValueError("Codex did not publish its final answer")
    return {"provider": "codex", "effective_model": request["model"]}


def run_ollama(root, request, output, upstream):
    model = request["model"].removeprefix("ollama:")
    content = {"role": "user", "content": request["prompt"]}
    if request.get("images"):
        content["images"] = [base64.b64encode(local_file(root, name).read_bytes()).decode()
                             for name in request["images"]]
    messages = ([{"role": "system", "content": request["system"]}] if request.get("system") else []) + [content]
    options = request.get("options", {})
    payload = {"model": model, "messages": messages, "stream": False,
               "think": options.get("think", False), "keep_alive": options.get("keep_alive", "5m")}
    if request.get("schema"):
        payload["format"] = request["schema"]
    elif options.get("json_output"):
        payload["format"] = "json"
    payload["options"] = {key: options[key] for key in ("temperature", "num_predict", "num_ctx", "seed") if key in options}
    api = options.get("api", "chat")
    if api not in {"chat", "generate"}:
        raise ValueError("Ollama operation must be chat or generate")
    if api == "generate":
        payload.pop("messages")
        payload.update(prompt=request["prompt"], system=request.get("system", ""))
        if content.get("images"):
            payload["images"] = content["images"]
    answer = http_json(upstream.rstrip("/") + "/api/" + api, payload, options.get("timeout_seconds", 7500))
    text = str(answer.get("response") or answer.get("message", {}).get("content") or "")
    if not text.strip():
        raise ValueError("Ollama returned no answer")
    output.write_text(text, encoding="utf-8")
    runtime = {"provider": "ollama", "requested_alias": model,
               "effective_alias": answer.get("model", model), "effective_model": answer.get("model", model)}
    for key in ("done_reason", "eval_count", "prompt_eval_count", "total_duration"):
        if key in answer:
            runtime[key] = answer[key]
    return runtime


def run_comfyui(root, request, output, upstream):
    """Execute a packaged graph; ordered input bindings are explicit node/input pairs."""
    from copy import deepcopy
    from urllib.parse import urlencode
    upstream = upstream.rstrip("/")
    graph = deepcopy(request["workflow"])
    available = http_json(upstream + "/object_info")
    missing = {value.get("class_type") for value in graph.values()} - set(available)
    if missing:
        raise ValueError("ComfyUI is missing required nodes: " + ", ".join(sorted(missing)))
    for binding in request.get("bindings", []):
        path = local_file(root, binding["file"])
        boundary = "proxy" + uuid4().hex
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{path.name}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + path.read_bytes()
        data += f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\nfalse\r\n--{boundary}--\r\n'.encode()
        req = Request(upstream + "/upload/image", data=data,
                      headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        with urlopen(req, timeout=60) as response:
            uploaded = json.load(response)
        image = "/".join(part for part in (uploaded.get("subfolder", ""), uploaded["name"]) if part)
        graph[str(binding["node"])]["inputs"][binding.get("input", "image")] = image
    # Persist exact submission identity before sending; an ambiguous submission is never replayed.
    prompt_id = str(uuid4())
    write_json(root / "comfyui_submission.json", {"prompt_id": prompt_id, "settled": False, "workflow": graph})
    accepted = http_json(upstream + "/prompt", {"prompt": graph, "prompt_id": prompt_id, "client_id": str(uuid4())})
    prompt_id = accepted.get("prompt_id", prompt_id)
    deadline = time.monotonic() + float(request.get("options", {}).get("timeout_seconds", 7500))
    while time.monotonic() < deadline:
        entry = http_json(upstream + "/history/" + prompt_id).get(prompt_id)
        if entry:
            write_json(root / "comfyui_submission.json", {"prompt_id": prompt_id, "settled": True, "workflow": graph})
            write_json(root / "comfyui_history.json", entry)
            status = entry.get("status", {})
            if status.get("status_str") not in {"success", "succeeded"}:
                raise RuntimeError("ComfyUI execution failed: " + json.dumps(status)[-2000:])
            images = [image for value in entry.get("outputs", {}).values() for image in value.get("images", [])]
            if not images:
                raise ValueError("ComfyUI returned no output image")
            with urlopen(upstream + "/view?" + urlencode(images[-1]), timeout=60) as response:
                output.write_bytes(response.read())
            return {"provider": "comfyui", "effective_model": request["model"], "prompt_id": prompt_id}
        time.sleep(1)
    raise TimeoutError("ComfyUI settlement timed out; submission identity retained")


def legacy_request(root, ask, provider):
    """Read already-published text envelopes without rewriting immutable inputs."""
    return {"version": 1, "provider": provider,
            "model": ask.get("model") or ask.get("ollama_model", ""),
            "prompt": local_file(root, ask["prompt_file"]).read_text(encoding="utf-8"),
            "system": ask.get("ollama_request_options", {}).get("system", ""),
            "images": ask.get("image_files", []), "schema": ask.get("response_schema"),
            "output": ask.get("expected_output", "answer.json"),
            "options": {"think": ask.get("ollama_think", False), "temperature": ask.get("ollama_temperature", 0.2)}}


def execute(root: Path, provider: str, *, codex=None, ollama=None, comfyui=None) -> bool:
    ask = json.loads((root / "ask_manifest.json").read_text(encoding="utf-8"))
    path = root / "ai_request.json"
    request = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else legacy_request(root, ask, provider)
    result = {"version": 1, "ask_id": ask["ask_id"], "worker_id": "ai-proxy-" + provider,
              "asset_id": ask.get("asset_id"), "source_ask_id": ask.get("source_ask_id", ""),
              "expected_output": request.get("output", "answer.json"), "started_at": iso_utc(utc_now()),
              "ollama_attempt_id": ask.get("ollama_attempt_id", ask["ask_id"]), "requested_model": request.get("model", "")}
    try:
        if request.get("version") != 1 or request.get("provider") != provider:
            raise ValueError("Request version/provider does not match trusted worker")
        output = local_file(root, result["expected_output"])
        if provider == "codex":
            runtime = run_codex(root, request, output, codex)
        elif provider == "ollama":
            runtime = run_ollama(root, request, output, ollama or os.environ.get("AI_PROXY_OLLAMA_UPSTREAM", "http://127.0.0.1:11434"))
        else:
            runtime = run_comfyui(root, request, output, comfyui or os.environ.get("AI_PROXY_COMFYUI_UPSTREAM", "http://127.0.0.1:8188"))
        if request.get("schema"):
            value = json.loads(output.read_text(encoding="utf-8"))
            validate_structure(value, request["schema"])
        result.update(status="SUCCESS", runtime=runtime, effective_model=runtime.get("effective_model", ""))
        if provider == "comfyui":
            write_json(root / "LOCAL_RENDER_METADATA.json", {**request.get("render_metadata", {}), **runtime,
                       "started_at": result["started_at"], "completed_at": iso_utc(utc_now()), "status": "SUCCESS"})
        if provider == "ollama":
            result["ollama_runtime"] = runtime
    except Exception as exc:
        result.update(status="FAILED", error_type=type(exc).__name__, error_message=str(exc)[-4000:])
    result["completed_at"] = iso_utc(utc_now())
    write_json(root / "answer_manifest.json", result)
    write_json(root / "ai_result.json", result)
    return result["status"] == "SUCCESS"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--provider", required=True, choices=("codex", "ollama", "comfyui"))
    args = parser.parse_args()
    execute(args.job_dir, args.provider)


if __name__ == "__main__":
    main()
