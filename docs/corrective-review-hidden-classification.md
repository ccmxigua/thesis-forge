# Hidden semantic correction during retry preparation

A full independent result can first fail for an empty source inventory while
another check already contains a source-bound classification correction.
Validating that sibling before the next invocation used to throw its correction
into the bridge with no new request/raw/compilation artifacts. The bridge
correctly refused to grant a primary edit from that incomplete invocation.

Scope preparation now keeps two existing structured check-local corrections
fresh: existing-content verification classification and external-action pending
disposition. It replays the immutable parent source packet and compilation,
requires exactly one correction for the current check and an unchanged rejected
result, and records the complete correction payload and digest in the scope
proof. Those checks are never retained as validated siblings.

This does not authorize a primary edit, a new budget or a pass projection. The
ordinary bounded second independent invocation must actually run, persist its
own evidence and undergo all source/schema/reference/semantic validators. A
reproduced classification correction can then reach the existing bridge
authorization handler using that new complete evidence. Pending human duties
remain pending. Persistent or additional errors still reject the result.

Unknown errors, invalid quotations, references, schemas, stale parent identity,
nonlocal/duplicate correction identities and mutation during validation remain
fatal. Persisted proof consumers recompute the full correction record; changing
its payload and resealing its local hashes cannot make it current evidence.
Consumer replay includes missing-inventory feedback as well as the existing
empty-verdict, unsafe-uncertainty and typed-alignment paths. The legacy full-read
fallback without captured parent artifacts cannot claim a retention proof.

Tests cover simultaneous missing inventory and hidden correction, the two
correction types, transport invocation with persisted rejected artifacts,
source/reference/schema damage, identity/mutation faults and resealed proof
tampering. Captured BSU replay does not imply that the historical run passed;
fresh BSU and editable DOCX/Word acceptance remain separate gates.
