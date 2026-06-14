<!-- Stage B — headless author, first round. Rendered with: {{brief}},
     {{highlevel_plan}}. Claude's stdout (.result) is persisted to
     20-impl-plan.md by the orchestrator. -->

Write a detailed **implementation plan** that turns the approved high-level plan
into something a fresh engineer (or a fresh coding agent) could build without
further questions.

Brief:
<brief>
{{brief}}
</brief>

Approved high-level plan:
<highlevel_plan>
{{highlevel_plan}}
</highlevel_plan>

The implementation plan must cover:

- **Architecture** — components, their responsibilities, and how they interact.
- **Data shapes / interfaces** — key types, schemas, API surfaces, file formats.
- **Work breakdown** — an ordered list of concrete, independently-verifiable
  steps. Each step: what to build, where, and how you'll know it works.
- **Dependencies & tech choices** — with one-line justifications.
- **Testing strategy** — what gets tested and how.
- **Risks & mitigations**, and explicit **non-goals**.

Be specific enough that the build step needs no further design decisions. Output
the plan as Markdown only — no preamble, no sign-off.
