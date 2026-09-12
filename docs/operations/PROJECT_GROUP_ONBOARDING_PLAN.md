# Project and Telegram-group onboarding plan

Status: planned separate package  
Last updated: 2026-09-12

This plan reserves the next product slice. The current `/projects` menu lists
registered projects and states that creation is unavailable. No project, group,
credential, permission, or deployment change is part of session-connect.

## Proposed boundary

1. Add **Create project** to the owner-only Hub private `/projects` menu only
   after every following acceptance boundary is implemented.
2. Telegram may select only a locally prepared immutable project ID from a
   bounded registry of candidates. Root preparation and allowlisting remain a
   local action; Telegram never accepts a path.
3. Recheck the current official Telegram capability before implementation. The
   Bot API documents
   [`createForumTopic`](https://core.telegram.org/bots/api#createforumtopic)
   inside an existing forum group but does not currently establish the complete
   private supergroup/forum topology required here. If that remains true, give
   the owner a bounded manual group-creation step. Do not introduce a hidden
   user-account client.
4. Verify private-supergroup/forum mode, numeric identity, owner membership,
   minimum bot permissions, Privacy Mode deployment instructions, and absence
   of an existing binding before any registry mutation.
5. Bind the prepared immutable `project_id` to the exact numeric group only
   through a local durable confirmation. Then verify root resolution, group
   readiness and a topic canary.
6. Persist an onboarding workflow with external-operation receipts. Repeating a
   completed step returns its result. An unknown group/topic creation or binding
   outcome pauses for inspection and never creates or deletes another Telegram
   object automatically.
7. Treat any user-account session, additional bot token, group-creation
   authority, or broader permission as new authority requiring a separate owner
   decision. The acceptance actor remains test-only unless the owner explicitly
   changes that product boundary.

## Acceptance outline

- crafted titles, callbacks, forwards and private text cannot select a root;
- duplicate and concurrent create requests produce one durable project binding;
- partial local preparation and unknown Telegram outcomes recover without
  duplicate groups or silent deletion;
- a pre-existing numeric group binding cannot be stolen by title reuse;
- disabled or non-Git projects and roots outside `allowed_roots` fail closed;
- minimum bot permissions are demonstrated in a deployment-local canary;
- rollback retains the immutable project/group mapping and all uncertainty
  evidence.

Implementation should begin with a fresh official Bot API review and failing
state-machine tests. It must remain a separate checked package from this plan.
