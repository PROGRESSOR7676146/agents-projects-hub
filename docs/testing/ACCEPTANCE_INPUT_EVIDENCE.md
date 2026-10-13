# Acceptance input evidence

The [dedicated actor procedure](README.md#dedicated-acceptance-user) owns setup
and execution. These additive private result fields help assess what a run
actually observed; they do not grant new actor or service authority.

| Field | Meaning |
| --- | --- |
| `input_message_ids` | Telegram IDs returned for the sent inputs. Empty means unrecorded, not that no input was sent. |
| `provider_job_observed` | `true`: a durable job found for a control input. `false`: both expected control replies and empty exact-input state reads. `null`: absent or incomplete observation. |

The simple response checks (`status`, `accounts`, `model_menu`, `provider_ping`,
`reply_route`) record their input IDs on success and response failure. Reply
routing records both parent and follow-up inputs when both have been sent.
These checks do not inspect live SQLite state and leave job observation `null`.
Other scenarios retain the default empty input list unless explicitly recorded.

Only the final, separately enabled P0/P1 control check reads job membership for
the exact chat/message pair after each expected `/status` and `/accounts` reply.
A job in any status fails the check; a job for `/status` prevents sending
`/accounts`. Other chats or inputs do not affect this result. A reply or state
failure leaves observation unknown, even after an earlier empty state read.
This proves job absence only at the observation time, not absence of every RPC
or future admission. Expected replies alone are not job-absence evidence.

`tests.test_acceptance_input_evidence` exercises the private JSON report, timeout
input retention, real read-only SQLite membership, unrelated jobs, every normal
job status, incomplete observation and restoration failure composition. It uses
fictional transports/state, with no Telegram, provider or service access.

The simple send/response path is isolated in `acceptance_actor._run_response_check`;
the final control subscenario is isolated in `p0_p1_acceptance._check_read_only_controls`.
The parent P0/P1 runner retains the fixed result order and sole service restoration
boundary. Replacing a failed result preserves its original evidence fields.
