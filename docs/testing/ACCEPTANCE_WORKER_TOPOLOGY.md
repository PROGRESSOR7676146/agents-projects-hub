# Restart acceptance worker topology

The [dedicated actor procedure](README.md#dedicated-acceptance-user) owns setup
and execution. The stock restart scenario supports only the fixed single Codex
consumer: stopping one worker cannot hold a recovery input queued while other
workers can lease it.

Before any canary action and at recovery boundaries, a passive systemd check
rejects active or transitional numbered Codex slots and unreadable metadata.
The fixed worker must be listed at active boundaries. While deliberately stopped,
it may be absent because systemd collected the unloaded unit; an empty valid
listing then passes. A listed fixed worker must remain inactive/failed at those
stopped boundaries. The guard never stops additional slots or accepts configurable
service names. The parent scenario retains restoration of its fixed units.

An owner-coordinated maintenance window must drain/exclude other consumers,
including manual or custom workers, and prevent concurrent topology changes.
This sampled guard is not a runtime fence or proof that an unlisted process
cannot consume work. Multiple-worker restart acceptance remains open; do not
run the stock scenario on an undrained worker pool.

Offline regressions cover refusal before Telegram/state/service effects,
topology drift at recovery boundaries, and restoration after worker stop.
They use fictional service metadata and transports, never live services.
