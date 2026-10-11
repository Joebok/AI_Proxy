# System Prompt Review

## Goal

Preserve AI_Proxy as a transport and execution boundary. ModelUpdater aliases select managed capabilities without embedding task-specific `SYSTEM` instructions. Task behavior remains owned by subscribers such as Zet.

## Required review

- Do not add a proxy-wide or model-alias system prompt.
- Do not infer task behavior from an Ollama model name or worker registration.
- Preserve subscriber-supplied job data without rewriting prompt content.

Zet currently sends self-contained feature prompts and has not added a `system_prompt_file` manifest field. AI_Proxy treats subscriber JSON as opaque apart from recursive path-safety validation. If a prompt-file extension is added later, require that:

- The relative file is included in the job inventory and moves with the job folder.
- `_file` path validation rejects absolute paths and traversal.
- Older jobs without the field continue to run.
- The proxy does not read, merge, default, log, or otherwise interpret prompt content.

## Deferred extension checks

These checks apply only if a subscriber later adds `system_prompt_file`:

- A queued job containing a safe relative `system_prompt_file` passes validation and reaches the registered worker unchanged.
- Missing inventoried prompt files wait or fail consistently with other job files.
- Absolute and parent-traversal `system_prompt_file` values are rejected.
- Logs and error messages do not expose prompt contents.

## Ownership boundary

- ModelUpdater owns managed alias definitions and runtime parameters.
- AI_Proxy owns safe transport, scheduling, and worker execution.
- Zet owns role-to-alias selection, feature prompts, schemas, task inputs, and output validation.
