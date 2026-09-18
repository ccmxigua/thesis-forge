"""Host-runtime identity and adapter-selection primitives.

The deterministic pipeline is intentionally host-neutral.  A process may only
use an automatic adapter when the host that launched it has declared the same
runtime through the trusted launcher environment.  Missing or conflicting
identity is an error; it is never resolved by selecting a recent session or a
different installed host.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Mapping


HOST_RUNTIME_ENV = "THESIS_FORGE_HOST_RUNTIME"
HOST_RUNTIME_VERSION_ENV = "THESIS_FORGE_HOST_RUNTIME_VERSION"
HOST_INVOCATION_ENV = "THESIS_FORGE_HOST_INVOCATION_ID"
PARENT_SESSION_ENV = "THESIS_FORGE_PARENT_SESSION_ID"


class HostRuntimeError(RuntimeError):
    """Base class for host identity and adapter errors."""


class HostRuntimeMismatch(HostRuntimeError):
    """The declared host does not match the expected host."""


class HostAdapterUnavailable(HostRuntimeError):
    """No native automatic adapter is implemented for the current host."""


@dataclass(frozen=True)
class HostRuntimeContext:
    runtime: str | None
    version: str | None
    invocation_id: str | None
    parent_session_id: str | None
    identity_evidence_source: str
    verification_status: str

    def as_audit(self) -> dict[str, str | None]:
        return {
            "host_runtime": self.runtime,
            "host_runtime_version": self.version,
            "host_invocation_id": self.invocation_id,
            "parent_session_id": self.parent_session_id,
            "identity_evidence_source": self.identity_evidence_source,
            "verification_status": self.verification_status,
        }


def _normalize_runtime(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", normalized):
        raise HostRuntimeError(
            "host runtime must be a lowercase identifier containing only "
            "letters, digits, '.', '_' or '-'")
    return normalized


def inspect_host_runtime(
    expected: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    require: bool = False,
) -> HostRuntimeContext:
    """Read a launcher-supplied host identity without guessing.

    The value is deliberately not inferred from installed binaries, model
    names, the current shell, or recent sessions.  Those signals cannot prove
    which agent owns the invoking conversation.
    """
    env = os.environ if environ is None else environ
    actual = _normalize_runtime(env.get(HOST_RUNTIME_ENV))
    expected_runtime = _normalize_runtime(expected)
    if actual is None:
        if require or expected_runtime is not None:
            raise HostRuntimeError(
                f"{HOST_RUNTIME_ENV} is missing; refusing automatic host dispatch")
        return HostRuntimeContext(
            runtime=None,
            version=None,
            invocation_id=None,
            parent_session_id=None,
            identity_evidence_source="unavailable",
            verification_status="unavailable",
        )
    if expected_runtime is not None and actual != expected_runtime:
        raise HostRuntimeMismatch(
            f"host runtime mismatch: expected {expected_runtime}, observed {actual}")
    return HostRuntimeContext(
        runtime=actual,
        version=(env.get(HOST_RUNTIME_VERSION_ENV) or "").strip() or None,
        invocation_id=(env.get(HOST_INVOCATION_ENV) or "").strip() or None,
        parent_session_id=(env.get(PARENT_SESSION_ENV) or "").strip() or None,
        identity_evidence_source=f"environment:{HOST_RUNTIME_ENV}",
        # A launcher declaration is auditable, but the repository cannot turn
        # an arbitrary environment variable into cryptographic proof of the
        # surrounding desktop application.
        verification_status="declared",
    )


def require_host_runtime(
    expected: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> HostRuntimeContext:
    return inspect_host_runtime(expected, environ=environ, require=True)


def automatic_adapter_id(context: HostRuntimeContext) -> str:
    """Return only an adapter explicitly implemented for ``context``."""
    if context.runtime == "openclaw":
        return "openclaw"
    if context.runtime == "codex":
        return "codex"
    if context.runtime is None:
        raise HostAdapterUnavailable(
            "no host runtime is declared; use the current host's packet workflow "
            "or provide a native launcher declaration")
    raise HostAdapterUnavailable(
        f"no automatic adapter is implemented for host runtime {context.runtime!r}; "
        "use --prepare-agent-review so the current host Agent handles the packets")


def require_parent_session(context: HostRuntimeContext, explicit: str | None = None) -> str:
    """Resolve a parent binding only from explicit or launcher-provided data."""
    selected = (explicit or context.parent_session_id or "").strip()
    if not selected:
        raise HostRuntimeError(
            "parent session binding is missing; refusing to select a recent or global session")
    return selected
