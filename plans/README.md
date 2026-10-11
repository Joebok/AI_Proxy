# Implementation planning convention

Substantial features, complex bug fixes, cross-component changes and refactors
require a written plan before application code edits. Trivial edits do not.
Use [TEMPLATE.md](TEMPLATE.md), save `active/YYYY-MM-DD-short-topic.md`, and move
to `completed/` when acceptance is met or the task is explicitly closed with
remaining work recorded. An unfinished/blocked plan remains active. No existing
repository plan/tracker convention was found to replace.

## Luna-sized work packets

Break every plan into ordered packets small enough for a smaller coding agent
to execute without prior chat history. This is a sizing convention, not an
instruction to delegate or choose a model; use one agent unless requested.
Each packet has one concrete outcome, bounded file/module responsibility,
dependencies, named contracts to preserve, an acceptance checkpoint and the
smallest relevant validation. Split packets that mix unrelated behavior or
require discovering multiple subsystems while implementing. No fixed time/token
budget is assumed.

Read [AGENTS.md](../AGENTS.md), [knowledge index](../Docs/INDEX.md), relevant docs,
source and tests before drafting. Make current and desired behavior explicit;
include interface/data/consumer/test/doc impact. Plans must be executable by
another agent with no original conversation. Do not invent commands, historical
intent or assumptions; record uncertainties and how they will be resolved.

Maintain progress/dependencies/deviations during implementation. Validate each
checkpoint, then record final changed paths, exact observed checks, failures and
unverified acceptance. Update only affected documentation and add ADRs where
durable design reasoning matters. Archive completed plan and keep its relative
links valid after moving it. The template uses `../../` links so active/completed
plans resolve to repository root from either directory.

This directory is an implementation record, not a duplicate project backlog.
No GitHub/GitLab issue or external write is required for local planning.
