<!-- Stage C — headless author, edit mode, inside the 30-impl/ worktree.
     Rendered with: {{brief}}, {{impl_plan}}. Run with --permission-mode
     acceptEdits and --add-dir <run>/30-impl. The artifact is the resulting diff. -->

Implement the project according to the approved implementation plan. You are
working inside a dedicated worktree; make real file changes here.

Brief:
<brief>
{{brief}}
</brief>

Approved implementation plan:
<impl_plan>
{{impl_plan}}
</impl_plan>

Rules:

- Follow the plan's work breakdown in order. Build it for real — working code,
  not stubs, unless the plan explicitly scopes something out.
- Match the conventions of any existing code in the worktree.
- Add the tests the plan calls for, and make them pass.
- Keep commits/changes scoped and coherent — an independent reviewer will review
  your diff against the base branch.
- When done, briefly summarize what you built and how you verified it.
