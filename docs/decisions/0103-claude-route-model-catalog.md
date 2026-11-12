# ADR 0103: Claude model catalog from the route's model list

Status: accepted, 2026-11-12 (owner request). Source implementation is separate from installed acceptance.

## Context

Without an explicit `model_catalog`, the Claude model menu offered only the
configured default model. The owner asked for a normal catalog. The CPA route
already publishes its model list at `GET /v1/models`; that list also contains
aliases under `claude-…` names that route a Claude client to other providers.

## Decision

1. An explicit `model_catalog` keeps priority.
2. Otherwise, when the host profile names a private `route_environment` and the
   agent uses the CPA route, the monitor's catalog refresh reads that loopback
   route's model list (metadata only: no inference, no proxy, no redirect,
   bounded response). It keeps only `owned_by: anthropic` models named
   `claude-<fable|opus|sonnet|haiku>-<version>` and offers the newest of each
   family, plus the configured default. Fable, Opus and Sonnet offer every
   supported effort; Haiku offers the configured default effort only.
3. The Controller stays cache-only: it shows the saved route catalog when the
   agent still qualifies for route discovery and the catalog contains the
   default model and effort, else the configured fallback. A file lock makes the
   monitor's route store and the Controller's fallback store exclusive, and the
   fallback rechecks under it, so it never replaces a route catalog. A failed
   refresh keeps the last good catalog.
4. A configured default that the route lists under another provider fails the
   refresh visibly. Versions compare by major, minor (one or two digits) and
   date, with undated identifiers ranking as current. The request has a 15 s
   overall deadline, and HTTP protocol errors count as refresh failures.

## Consequences

- The menu follows the models the route actually serves without edits to
  configuration; older versions stay selectable through an explicit catalog.
- Availability, entitlement and billing remain unverified; a listed model can
  still be refused by the account.
- The monitor makes one loopback metadata request per catalog lifetime.
