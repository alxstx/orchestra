<!-- Generic revision template (Stage B/C). Rendered with: {{artifact_label}}
     (e.g. "implementation plan"), {{artifact_path}}, {{prev_artifact}},
     {{blocking_issues}} (formatted list from the verdict), {{round}}.
     Claude's stdout is persisted as the new artifact version. -->

You are revising the **{{artifact_label}}** below in response to an independent
reviewer. This is round {{round}}.

Current {{artifact_label}}:
<artifact>
{{prev_artifact}}
</artifact>

The reviewer raised these **blocking issues** — every one must be resolved:
<blocking_issues>
{{blocking_issues}}
</blocking_issues>

Revise the {{artifact_label}} to address all of them. Then, at the very end,
append a short section:

```
## Revision notes (round {{round}})
- B1: <how you addressed it>
- B2: <how you addressed it>
```

one bullet per blocking issue id. If you believe an issue is mistaken, still
address the underlying concern and explain your reasoning in the note rather than
ignoring it. Output the full revised {{artifact_label}} as Markdown.
