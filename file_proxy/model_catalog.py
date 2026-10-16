"""Provider capability metadata, independent of GPU admission and subscriber code."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .ai_worker import http_json, write_json
from .models import iso_utc, utc_now


class ModelCatalog:
    def __init__(self, path: Path, ollama="http://127.0.0.1:11434", comfyui="http://127.0.0.1:8188", codex_models=None):
        self.path, self.ollama, self.comfyui = path, ollama.rstrip("/"), comfyui.rstrip("/")
        self._lock = threading.Lock()
        if codex_models is None:
            from .runtime import RuntimeConfig
            codex_models = RuntimeConfig().codex_models
        self.codex_models = codex_models

    def read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("version") != 1
                    or not isinstance(value.get("models"), list)
                    or any(not isinstance(item, dict) for item in value["models"])):
                raise ValueError("Invalid cached provider catalog")
            return value
        except (OSError, ValueError):
            return {"version": 1, "models": [], "errors": {}, "refreshed_at": ""}

    def refresh(self):
        with self._lock:
            previous = self.read()
            entries = [{"id": "codex:" + model, "name": model, "provider": "codex",
                        "capabilities": ["text", "vision", "structured_json"], "worker": "ai_codex"}
                       for model in self.codex_models]
            errors = {}
            for provider in ("ollama", "comfyui"):
                try:
                    discovered = []
                    if provider == "ollama":
                        for value in http_json(self.ollama + "/api/tags", timeout=5).get("models", []):
                            name = str(value.get("name") or value.get("model") or "").strip()
                            if not name:
                                continue
                            capabilities = http_json(self.ollama + "/api/show", {"model": name}, timeout=5).get("capabilities", [])
                            discovered.append({"id": name, "name": name, "provider": provider, "worker": "ai_ollama",
                                               "capabilities": ["text", "structured_json"] + (["vision"] if "vision" in capabilities else [])})
                    else:
                        data = http_json(self.comfyui + "/object_info", timeout=5)
                        node_types = sorted(data)
                        def choices(node, name):
                            value = data.get(node, {}).get("input", {}).get("required", {}).get(name, [[]])
                            return value[0] if value and isinstance(value[0], list) else []
                        comfyui_options = {"checkpoints": choices("CheckpointLoaderSimple", "ckpt_name"),
                            "controlnet_models": choices("ControlNetLoader", "control_net_name"),
                            "samplers": choices("KSampler", "sampler_name"), "schedulers": choices("KSampler", "scheduler"),
                            "diffusion_models": choices("UNETLoader", "unet_name"),
                            "text_encoders": sorted(set(choices("CLIPLoader", "clip_name") + choices("DualCLIPLoader", "clip_name1") + choices("DualCLIPLoader", "clip_name2"))),
                            "vaes": choices("VAELoader", "vae_name"), "node_types": node_types}
                        model_names = sorted(set(comfyui_options["diffusion_models"] + comfyui_options["checkpoints"]))
                        discovered = [{"id": name, "name": name, "provider": provider, "worker": "ai_comfyui",
                                       "capabilities": ["image_generation"]} for name in model_names]
                    entries.extend(discovered)
                except Exception as exc:
                    errors[provider] = str(exc)
                    entries.extend(value for value in previous.get("models", []) if value.get("provider") == provider)
            catalog = {"version": 1, "models": sorted(entries, key=lambda value: (value["provider"], value["name"].casefold())),
                       "refreshed_at": iso_utc(utc_now()), "errors": errors,
                       "comfyui_node_types": locals().get("node_types", previous.get("comfyui_node_types", []))}
            catalog["comfyui_options"] = locals().get("comfyui_options", previous.get("comfyui_options", {}))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self.path, catalog)
            return catalog
