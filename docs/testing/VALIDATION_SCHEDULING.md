# Validation scheduling through Git hooks

The canonical test stage asks discovery for every test module, including nested
test packages, then runs each in its own process, several in parallel. Each test
runs in the module discovery found it in. The tests that start must be exactly
the discovered ones by test ID. Any module under `tests` that defines `load_tests`
is refused because the suite such a hook builds cannot be reproduced by an
isolated module run. The validator names every failing, empty, mismatched or
timed-out module after all modules finish; each module must pass on its own.
See [ADR 0041](../decisions/0041-parallel-isolated-test-modules.md).

The [canonical validator](README.md) accepts `--jobs 1..32` for the number of
isolated test-module processes. The unchanged installed Git hooks can receive
the same bounded scheduling choice through `HUB_VALIDATION_TEST_JOBS`:

```bash
HUB_VALIDATION_TEST_JOBS=4 git commit
HUB_VALIDATION_TEST_JOBS=4 git push
```

An explicit `--jobs` takes precedence. Without either input, CPU count capped
at eight remains the default. Focused runs ignore this environment setting.
The same number bounds the worker processes of the commit and canonical
privacy/history scan, which run at the lowest CPU priority so that a gate
never outcompetes a live Hub on the same host.
Invalid environment values in canonical/commit runs fail before any stages,
unless an explicit `--jobs` overrides the environment setting.

Lower concurrency can help short fixture waits under load. It changes no test
selection, discovery check, deadline, failure policy or required gate; it is
not evidence that a timing failure is harmless. Diagnose failures and retain
their logs. Test skips and partial focused checks remain subject to the
[existing evidence boundaries](README.md).
