---
name: triage
description: Use when classifying GitHub issues before drafting a comment.
allowed_tools:
  - get_issue
  - list_issues
---
# Triage playbook

1. Call `load_skill` only once at the start of triage work.
2. Fetch the issue with the allowed MCP tools (do not invent repository data).
3. Summarize severity and suggested next action.
4. Call `_submit` with the structured result required by the step prompt.
