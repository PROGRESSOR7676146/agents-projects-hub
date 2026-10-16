# Passive-forward acceptance observation

The [dedicated actor procedure](README.md#dedicated-acceptance-user) owns setup
and execution. The owning contracts are
[AC-F-011](../product/ONBOARDING_AND_ACCEPTANCE.md#15-functional-acceptance-criteria)
and [message semantics](../product/IDENTITY_AND_INTERACTION.md#message-semantics).

After forwarding a harmless provider marker, `forwarded_quote` observes five
seconds without an answer from any configured provider. Only a completed quiet
window permits the explicit context follow-up. Unrelated canary traffic or an
observation error fails the check before that follow-up. A failed result stops
the run before the next provider or scenario. The subsequent answer checks that
the forward is visible as quoted context.

The response poller uses a distinct timeout exception for a completed observation
window. Other actor errors, even with timeout-like text, cannot establish silence.
The whole fetched batch is checked for contamination before a response counts;
traffic in another numeric topic remains outside the observation.

Silence is a bounded Telegram observation. It does not prove absence of a native
invocation, token use or a later response. Deployment-local native evidence is
still needed for those claims; a source fix does not establish live acceptance.

`tests.test_acceptance_forward_silence` exercises the actual poller and forward
scenario with fictional message history and an injected clock. It covers clean
timeout, either provider answering, unrelated traffic including after a matched
answer, another topic, generic observation errors and fail-fast run termination.
It makes no Telegram, provider or service calls.
