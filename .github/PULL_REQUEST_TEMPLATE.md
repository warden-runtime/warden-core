## Summary

<!-- What changed and why? Link issues with "Fixes #123" when applicable. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactor / chore
- [ ] Enterprise plugin only (private **warden-enterprise** repo)

## Checklist

- [ ] `make check` passes (kernel changes; also lints `mcp/`)
- [ ] `make tests` passes (or targeted pytest paths for small fixes — note which)
- [ ] `make test-mcp` passes when `mcp/` changed
- [ ] Open-core boundary respected (`make check-boundary`; no `enterprise/` imports from kernel; `mcp/` must not import `common` / `engine` / `workers` / `cli`)
- [ ] Tests added or updated for behavior changes
- [ ] Docs updated (`docs/` and/or `website/` build if links or API surface changed)

## Notes for reviewers

<!-- Migration impact, breaking changes, follow-ups, screenshots for docs UI, etc. -->
