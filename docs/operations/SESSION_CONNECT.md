# Saved Codex session connection

Status: repository-complete; live acceptance pending  
Last updated: 2026-09-12

This procedure connects an existing Codex CLI conversation to one registered
Telegram project topic. It invokes no model during selection or activation.
Close the native CLI before confirming so Telegram becomes the only writer.

## Project-topic entry

1. In the intended registered topic, send `/connect`.
2. Select one bounded saved-session label for that project.
3. Review the source, destination, history separation, and replacement notice.
4. Close the CLI and choose **CLI закрыт — подключить**.
5. Wait for the Hub's service marker and success message. Send the next ordinary
   message after them; it resumes the selected conversation. Do not send
   `/return` for this flow.

An empty destination has no replacement warning. An occupied Codex destination
archives only its current Hub binding. A different active provider, local
writer, queued/running work, pending delivery, unresolved outcome, lane binding,
or changed session stops the workflow without partial replacement.

## Hub-private entry

Open the configured Hub bot in a private chat owned by an allowlisted owner:

- `/start` shows registered projects and the connection action;
- `/projects` lists registered projects; project/group creation is not available;
- `/connect` selects project, saved session, and destination;
- `/cancel` cancels the active workflow without changing a topic session.

For a new destination, choose **Новая тема** and send a 1–128 character display
title. The bot creates it only in the project's already registered forum group,
using Telegram's numeric result as identity. Arbitrary private text never becomes
a provider turn.

If the Hub cannot determine whether Telegram created the topic, it will not
retry. Inspect the registered group. If the topic exists, start `/connect` in
that topic; otherwise begin a new private workflow. Do not edit SQLite.

## Local assisted entry

Run with an explicit Hub configuration:

```text
agents-projects-hub session connect /home/example/.config/agents-projects-hub/hub.json
```

Select an owner when configuration has more than one, then a registered project
and a bounded saved-session label. The command prints an expiring
`/connect CODE`. Close the CLI and send that command either in the target project
topic or in the Hub private chat. Topic use goes directly to confirmation;
private use opens destination selection.

For scripted local selection, pass `--owner-user-id`, `--project`,
`--codex-thread-id`, and `--json`. Omitting the configuration returns
`configuration_required`; the helper never searches hidden user paths. Codes
expire after ten minutes, are owner/project/source scoped, and are consumed only
when marker activation commits.

## Failure and recovery boundaries

- Discovery/metadata failure: the current binding is unchanged; restart
  `/connect` after the supported Codex metadata transport is healthy.
- Stale selection or changed destination: start `/connect` again and review the
  new state.
- Unknown marker send: do not repeat automatically. Inspect the target topic and
  current Hub status before starting a new explicit connection.
- Unknown topic creation: inspect the registered group before another creation.
- Expired or invalid code: issue a fresh code locally; repeated failures are
  temporarily rate-limited.
- Controller/worker/sender restart: the SQLite workflow resumes from its durable
  stage. Completed code redemption returns the recorded result.

## Deployment-local live acceptance

After a separately authorized deployment of one exact clean revision, retain
private evidence for:

1. bot command-menu readback in the Hub private chat and registered group;
2. bounded session discovery with no prompt/path disclosure;
3. empty-topic connection and exact history continuation;
4. occupied-Codex replacement with only the old Hub binding archived;
5. Hub-private existing-topic and new-topic journeys;
6. local code in a topic and in Hub private chat, including expiry/repeat;
7. Controller, worker, and sender restart at pre-marker and post-commit stages;
8. revoked Manage Topics permission and a controlled ambiguous transport failure;
9. `/local`, `/return`, `/new`, and another provider after the change;
10. every required long-running component reporting the same deployed Git SHA.

Fake provider and Telegram adapters are repository evidence only. Do not record
real project names, chat IDs, session IDs, credentials, screenshots, or
transcripts in this repository.
