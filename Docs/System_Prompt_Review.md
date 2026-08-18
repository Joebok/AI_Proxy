# System Prompt Review

## Goal

Preserve AI_Proxy as a transport and execution boundary while ModelUpdater aliases stop embedding `SYSTEM` instructions. Task behavior remains owned by subscribers such as Zet.

## Required review

- Do not add a proxy-wide or model-alias system prompt.
- Do not infer task behavior from an Ollama model name or worker registration.
- Preserve subscriber-supplied Ollama request fields, including `system` and `role: system`, without rewriting their content.
- Keep any ask-manifest extension optional and backward compatible.

Zet may add a `system_prompt_file` field to `ask_manifest.json`. AI_Proxy currently treats subscriber JSON as opaque apart from recursive path-safety validation. Confirm that:

- The relative file is included in the job inventory and moves with the job folder.
- `_file` path validation rejects absolute paths and traversal.
- Older jobs without the field continue to run.
- The proxy does not read, merge, default, log, or otherwise interpret prompt content.

## Tests to add or confirm

- A queued job containing a safe relative `system_prompt_file` passes validation and reaches the registered worker unchanged.
- Missing inventoried prompt files wait or fail consistently with other job files.
- Absolute and parent-traversal `system_prompt_file` values are rejected.
- HTTP forwarding preserves `/api/generate` `system` fields and `/api/chat` system messages.
- Logs and error messages do not expose prompt contents.

## Ownership boundary

- ModelUpdater owns model selection and runtime parameters.
- AI_Proxy owns safe transport, scheduling, and worker execution.
- Zet owns feature prompts, schemas, task inputs, and output validation.
