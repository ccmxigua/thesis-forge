# Permission failure diagnostics

The BSU run `346d5e60-8ee0-4d94-b409-de642c777de3` stopped on chunk 3's
native `max_output_tokens` failure. Chunk 2 also recorded `PermissionError:
[Errno 1] Operation not permitted` without a traceback. The surviving files
do not establish which operation was denied. In particular, missing native
stdout/stderr does not distinguish process startup from cancellation cleanup.
Historical artifacts are retained unchanged; new diagnostics cannot recover a
past traceback or establish that permission is now granted.

Generic independent-review failures now retain exception frame coordinates,
errno/filenames, explicit causes and implicit contexts in `coverage-audit.json`.
The terminal host failure audit retains the same diagnostic graph. Both chains
matter: a cleanup failure can replace a cancellation exception while retaining
it as context. Capture excludes frame locals, source lines, command arguments
and environment. It does not copy exception messages, which can embed argv;
the pre-existing outer error fields remain unchanged.
Chain/frame bounds are explicit; unavailable frames remain empty, not inferred.

These fields are diagnostic only. Permission failures still propagate and are
nonretryable, no model/provider fallback is introduced, and submission remains
blocked. If writing the diagnostic audit also fails, the exception continues
to the existing outer failure handler; no alternate write location or tool is
used to bypass the denial. A new live run still requires normal authorization
and resolution of any explicit execution refusal. Offline injected exceptions
are not evidence of the historical cause or of live permission recovery.
