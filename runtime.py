"""
Phase 8-2.1 runtime wiring.

Phase 8-2 shipped the pieces (RoundRobinSelector, RequestRoundRobinRunner,
LocalWorkspace) but no production entry point assembled them, so nothing outside
the tests ever enabled request-level round-robin. This module is that single
integration point: the CLI and any embedder build the stack through these
factories instead of hand-constructing the selector and runner per request.

Design rules:
  * Round-robin is strictly opt-in (``config.request_round_robin``); when it is
    off the client is a plain key-pool client and the legacy environment_id
    flow is untouched.
  * The local workspace, not the model, owns the files.
"""
from typing import Any, List, Optional, Tuple

from client import AntigravityClient
from keys import KeyPoolManager
from round_robin import RequestRoundRobinRunner, RoundRobinSelector, selector_from_config


class RuntimeWiringError(Exception):
    """Raised when the runtime is asked for a flow it is not configured for."""


def build_client(
    config: Any,
    key_pool: Optional[KeyPoolManager] = None,
    registry: Optional[Any] = None,
    session_manager: Optional[Any] = None,
    rr_storage_path: Optional[str] = None,
) -> Tuple[AntigravityClient, KeyPoolManager, Optional[RoundRobinSelector]]:
    """Assemble a client, enabling round-robin only when the config says so."""
    pool = key_pool or KeyPoolManager(
        cooldown_seconds=getattr(config, "cooldown_seconds", 60)
    )
    selector = selector_from_config(config, pool, storage_path=rr_storage_path)
    model = getattr(config, "model", None)
    client = AntigravityClient(
        key_pool=pool,
        registry=registry,
        session_manager=session_manager,
        round_robin=selector,
        model=model,
    )
    return client, pool, selector


def build_runner(
    config: Any,
    workspace_root: str,
    key_pool: Optional[KeyPoolManager] = None,
    registry: Optional[Any] = None,
    session_manager: Optional[Any] = None,
    rr_storage_path: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Tuple[RequestRoundRobinRunner, AntigravityClient, KeyPoolManager,
           Optional[RoundRobinSelector]]:
    """Build the per-request stack used for local, file-based coding tasks."""
    from local_workspace import LocalWorkspace

    client, pool, selector = build_client(
        config, key_pool=key_pool, registry=registry,
        session_manager=session_manager, rr_storage_path=rr_storage_path,
    )
    workspace = LocalWorkspace(
        workspace_root,
        state_dir=state_dir,
        max_file_size_bytes=getattr(config, "max_file_size_bytes", 512 * 1024),
    )
    runner = RequestRoundRobinRunner(client, workspace, selector=selector)
    return runner, client, pool, selector


def run_coding_request(
    config: Any,
    workspace_root: str,
    prompt: str,
    required_files: Optional[List[str]] = None,
    task_context: Optional[str] = None,
    timeout: int = 120,
    key_pool: Optional[KeyPoolManager] = None,
    rr_storage_path: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> dict:
    """Run one local-workspace coding request through the wired runtime."""
    if not getattr(config, "request_round_robin", False):
        raise RuntimeWiringError(
            "request_round_robin is disabled; the local-workspace flow requires it. "
            "Enable it in config/env (AGY_REQUEST_ROUND_ROBIN=1) to use this path."
        )
    runner, _client, _pool, _sel = build_runner(
        config, workspace_root, key_pool=key_pool,
        rr_storage_path=rr_storage_path, state_dir=state_dir,
    )
    return runner.run_request(
        prompt, required_files=required_files,
        task_context=task_context, timeout=timeout,
    )
