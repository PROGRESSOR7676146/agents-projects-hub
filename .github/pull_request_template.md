## Summary

Describe the behavior and trust boundary affected by this change.

## Verification

- [ ] `python scripts/validate.py`
- [ ] Privacy scan passes; this PR contains no real project names, deployment
      history, account identifiers, bot usernames, chat/topic IDs, invite links,
      owner-specific paths, or session transcripts
- [ ] New router behavior has tests
- [ ] No credentials, local paths, state, hidden reasoning, or terminal buffers
- [ ] Subprocesses use argument arrays and keep approvals fail-closed
- [ ] Documentation and acknowledgments are updated when applicable
