import re
"""
Observability layer for agy-router.
Provides structured logging, request correlation, secret redaction,
health checks, and diagnostics.
"""
import os
import sys
import json
import uuid
import time
import logging
import threading
from typing import Optional, Dict, Any, List, Union
from contextvars import ContextVar
from datetime import datetime, timezone

# Context variable for request_id correlation (thread-safe)
_request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
_session_id_var: ContextVar[Optional[str]] = ContextVar("session_id", default=None)

# Global logger instance
_logger: Optional[logging.Logger] = None
_logging_configured = False
_logging_lock = threading.Lock()


def configure_logging(log_level: str = "INFO", json_output: bool = False) -> logging.Logger:
    """
    Configure structured logging for agy-router.
    
    Args:
        log_level: Python logging level (DEBUG, INFO, WARNING, ERROR)
        json_output: If True, output JSON lines; if False, human-readable
    
    Returns:
        Configured logger instance
    """
    global _logger, _logging_configured
    
    with _logging_lock:
        if _logging_configured:
            return _logger
        
        _logger = logging.getLogger("agy_router")
        _logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        _logger.propagate = False
        
        # Clear any existing handlers
        _logger.handlers.clear()
        
        handler = logging.StreamHandler(sys.stderr)
        
        if json_output:
            formatter = JsonFormatter()
        else:
            formatter = HumanReadableFormatter()
        
        handler.setFormatter(formatter)
        _logger.addHandler(handler)
        _logging_configured = True
    
    return _logger


class JsonFormatter(logging.Formatter):
    """JSON formatter for structured logging output."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
            "request_id": _request_id_var.get(),
            "session_id": _session_id_var.get(),
        }
        
        # Add any extra fields from the log record
        for key, value in record.__dict__.items():
            if key not in ["name", "msg", "args", "levelname", "levelno", "pathname",
                          "filename", "module", "lineno", "funcName", "created",
                          "msecs", "relativeCreated", "thread", "threadName",
                          "processName", "process", "message", "event"]:
                if value is not None:
                    log_data[key] = value
        
        return json.dumps(log_data, ensure_ascii=False)


class HumanReadableFormatter(logging.Formatter):
    """Human-readable formatter for CLI output."""
    
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        event = getattr(record, "event", record.getMessage())
        request_id = _request_id_var.get()
        session_id = _session_id_var.get()
        
        parts = [f"[{timestamp}] {record.levelname}"]
        
        if request_id:
            parts.append(f"req={request_id}")
        if session_id:
            parts.append(f"sess={session_id}")
        
        parts.append(event)
        
        # Add extra fields
        extras = []
        for key, value in record.__dict__.items():
            if key not in ["name", "msg", "args", "levelname", "levelno", "pathname",
                          "filename", "module", "lineno", "funcName", "created",
                          "msecs", "relativeCreated", "thread", "threadName",
                          "processName", "process", "message", "event"]:
                if value is not None:
                    extras.append(f"{key}={value}")
        
        if extras:
            parts.append(" ".join(extras))
        
        return " ".join(parts)


def get_logger() -> logging.Logger:
    """Get the configured logger instance."""
    global _logger
    if _logger is None:
        _logger = configure_logging()
    return _logger


def generate_request_id() -> str:
    """Generate a new unique request ID."""
    return f"req_{uuid.uuid4().hex[:16]}"


def get_request_id() -> Optional[str]:
    """Get the current request ID from context."""
    return _request_id_var.get()


def set_request_id(request_id: str) -> None:
    """Set the request ID for the current context."""
    _request_id_var.set(request_id)


def clear_request_id() -> None:
    """Clear the request ID from context."""
    _request_id_var.set(None)


def get_session_id() -> Optional[str]:
    """Get the current session ID from context."""
    return _session_id_var.get()


def set_session_id(session_id: str) -> None:
    """Set the session ID for the current context."""
    _session_id_var.set(session_id)


def clear_session_id() -> None:
    """Clear the session ID from context."""
    _session_id_var.set(None)


class RequestContext:
    """
    Context manager for request-scoped logging.
    Automatically generates and sets request_id, and cleans up on exit.
    """
    
    def __init__(self, request_id: Optional[str] = None, session_id: Optional[str] = None):
        self.request_id = request_id or generate_request_id()
        self.session_id = session_id
        self._prev_request_id = None
        self._prev_session_id = None
    
    def __enter__(self) -> "RequestContext":
        self._prev_request_id = _request_id_var.get()
        self._prev_session_id = _session_id_var.get()
        _request_id_var.set(self.request_id)
        if self.session_id:
            _session_id_var.set(self.session_id)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._prev_request_id:
            _request_id_var.set(self._prev_request_id)
        else:
            clear_request_id()
        if self._prev_session_id:
            _session_id_var.set(self._prev_session_id)
        else:
            clear_session_id()


# Secret redaction patterns
SECRET_PATTERNS = [
    # Google API keys
    (r'AIza[A-Za-z0-9_\-]{30,}', '***REDACTED_API_KEY***'),
    # Generic bearer tokens
    (r'Bearer\s+[A-Za-z0-9_\-\.]{20,}', 'Bearer ***REDACTED***'),
    # Authorization headers
    (r'Authorization:\s*[A-Za-z0-9_\-\.]{20,}', 'Authorization: ***REDACTED***'),
    # x-goog-api-key headers
    (r'x-goog-api-key:\s*[A-Za-z0-9_\-]{20,}', 'x-goog-api-key: ***REDACTED***'),
    # Generic password/secret patterns
    (r'(password|secret|token|credential|apikey|api_key)["\']?\s*[:=]\s*["\']?[A-Za-z0-9_\-\.]{10,}',
     r'\1=***REDACTED***'),
]

# Additional patterns for URL credentials
URL_CRED_PATTERN = re.compile(r'(https?://[^:\s]+:)([^@\s]+)(@)')

import re


def redact_secrets(text: str) -> str:
    """
    Redact secrets from text before logging.
    
    Args:
        text: Input text that may contain secrets
        
    Returns:
        Text with secrets redacted
    """
    if not text:
        return ""
    
    result = text
    
    # URL credentials
    result = URL_CRED_PATTERN.sub(r'\1***\3', result)
    
    # Apply secret patterns
    for pattern, replacement in SECRET_PATTERNS:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    
    return result


def redact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively redact secrets from dictionary.
    
    Args:
        data: Dictionary that may contain secrets
        
    Returns:
        Dictionary with secrets redacted
    """
    if not isinstance(data, dict):
        return data
    
    result = {}
    sensitive_keys = {
        'api_key', 'apikey', 'password', 'secret', 'token',
        'credential', 'authorization', 'x-goog-api-key',
        'bearer', 'access_token', 'refresh_token'
    }
    
    for key, value in data.items():
        key_lower = key.lower()
        if any(sensitive in key_lower for sensitive in sensitive_keys):
            result[key] = "***REDACTED***"
        elif isinstance(value, str):
            result[key] = redact_secrets(value)
        elif isinstance(value, dict):
            result[key] = redact_dict(value)
        elif isinstance(value, list):
            result[key] = [redact_dict(v) if isinstance(v, dict) else 
                          redact_secrets(v) if isinstance(v, str) else v 
                          for v in value]
        else:
            result[key] = value
    
    return result


# Event logging functions

def log_event(
    event: str,
    level: int = logging.INFO,
    duration_ms: Optional[int] = None,
    **kwargs
) -> None:
    """
    Log a structured event with request correlation.
    
    Args:
        event: Event name (e.g., 'interaction.start', 'key.rollover')
        level: Logging level
        duration_ms: Optional duration in milliseconds
        **kwargs: Additional fields to include in log
    """
    logger = get_logger()
    
    # Redact any secret-like values in kwargs
    redacted_kwargs = {}
    for k, v in kwargs.items():
        if isinstance(v, str):
            redacted_kwargs[k] = redact_secrets(v)
        elif isinstance(v, dict):
            redacted_kwargs[k] = redact_dict(v)
        else:
            redacted_kwargs[k] = v
    
    if duration_ms is not None:
        redacted_kwargs["duration_ms"] = duration_ms
    
    logger.log(level, event, extra={"event": event, **redacted_kwargs})


def log_interaction_start(
    project_id: str,
    session_id: str,
    key_ref: str,
    tenant_id: Optional[str] = None,
    environment_id: Optional[str] = None
) -> None:
    """Log interaction start event."""
    log_event(
        "interaction.start",
        project_id=project_id,
        session_id=session_id,
        key_ref=key_ref,
        tenant_id=tenant_id,
        environment_id=environment_id
    )


def log_interaction_success(
    project_id: str,
    session_id: str,
    key_ref: str,
    tenant_id: Optional[str],
    environment_id: str,
    interaction_id: str,
    status_code: int = 200,
    duration_ms: Optional[int] = None
) -> None:
    """Log interaction success event."""
    log_event(
        "interaction.success",
        level=logging.INFO,
        duration_ms=duration_ms,
        project_id=project_id,
        session_id=session_id,
        key_ref=key_ref,
        tenant_id=tenant_id,
        environment_id=environment_id,
        interaction_id=interaction_id,
        status_code=status_code
    )


def log_interaction_failure(
    project_id: str,
    session_id: str,
    key_ref: str,
    tenant_id: Optional[str],
    environment_id: Optional[str],
    error: Union[str, Dict[str, Any]],
    status_code: Optional[int] = None,
    duration_ms: Optional[int] = None
) -> None:
    """Log interaction failure event."""
    error_str = str(error) if error else "unknown"
    log_event(
        "interaction.failure",
        level=logging.ERROR,
        duration_ms=duration_ms,
        project_id=project_id,
        session_id=session_id,
        key_ref=key_ref,
        tenant_id=tenant_id,
        environment_id=environment_id,
        error=redact_secrets(error_str),
        status_code=status_code
    )


def log_key_selected(
    key_ref: str,
    tenant_id: Optional[str],
    project_id: Optional[str] = None,
    session_id: Optional[str] = None
) -> None:
    """Log key selection event."""
    log_event(
        "key.selected",
        key_ref=key_ref,
        tenant_id=tenant_id,
        project_id=project_id,
        session_id=session_id
    )


def log_key_rollover(
    from_key_ref: str,
    to_key_ref: str,
    reason: str,
    tenant_id: Optional[str],
    project_id: Optional[str] = None,
    session_id: Optional[str] = None
) -> None:
    """Log key rollover event."""
    log_event(
        "key.rollover",
        level=logging.WARNING,
        from_key_ref=from_key_ref,
        to_key_ref=to_key_ref,
        reason=reason,
        tenant_id=tenant_id,
        project_id=project_id,
        session_id=session_id
    )


def log_key_cooldown(
    key_ref: str,
    cooldown_seconds: int,
    reason: str,
    tenant_id: Optional[str],
    project_id: Optional[str] = None,
    session_id: Optional[str] = None
) -> None:
    """Log key cooldown event."""
    log_event(
        "key.cooldown",
        level=logging.WARNING,
        key_ref=key_ref,
        cooldown_seconds=cooldown_seconds,
        reason=reason,
        tenant_id=tenant_id,
        project_id=project_id,
        session_id=session_id
    )


def log_key_inactive(
    key_ref: str,
    reason: str,
    tenant_id: Optional[str],
    project_id: Optional[str] = None,
    session_id: Optional[str] = None
) -> None:
    """Log key inactive event."""
    log_event(
        "key.inactive",
        level=logging.ERROR,
        key_ref=key_ref,
        reason=reason,
        tenant_id=tenant_id,
        project_id=project_id,
        session_id=session_id
    )


def log_session_created(
    session_id: str,
    project_id: str,
    bound_key: str,
    tenant_id: Optional[str],
    environment_id: Optional[str] = None
) -> None:
    """Log session created event."""
    log_event(
        "session.created",
        session_id=session_id,
        project_id=project_id,
        bound_key=bound_key,
        tenant_id=tenant_id,
        environment_id=environment_id
    )


def log_session_activated(
    session_id: str,
    project_id: str,
    bound_key: str,
    tenant_id: Optional[str],
    environment_id: Optional[str],
    last_interaction_id: Optional[str] = None
) -> None:
    """Log session activated event."""
    log_event(
        "session.activated",
        session_id=session_id,
        project_id=project_id,
        bound_key=bound_key,
        tenant_id=tenant_id,
        environment_id=environment_id,
        last_interaction_id=last_interaction_id
    )


def log_session_invalidated(
    session_id: str,
    project_id: str,
    reason: Optional[str] = None
) -> None:
    """Log session invalidated event."""
    log_event(
        "session.invalidated",
        level=logging.WARNING,
        session_id=session_id,
        project_id=project_id,
        reason=reason
    )


def log_session_reset(
    session_id: str,
    project_id: str,
    reason: Optional[str] = None
) -> None:
    """Log session reset event."""
    log_event(
        "session.reset",
        level=logging.INFO,
        session_id=session_id,
        project_id=project_id,
        reason=reason
    )


def log_recovery_same_tenant(
    session_id: str,
    project_id: str,
    from_key_ref: str,
    to_key_ref: str,
    reason: str
) -> None:
    """Log same-tenant recovery event."""
    log_event(
        "recovery.same_tenant",
        level=logging.WARNING,
        session_id=session_id,
        project_id=project_id,
        from_key_ref=from_key_ref,
        to_key_ref=to_key_ref,
        reason=reason
    )


def log_recovery_cross_tenant(
    session_id: str,
    project_id: str,
    from_tenant: str,
    to_tenant: str,
    reason: str
) -> None:
    """Log cross-tenant recovery event."""
    log_event(
        "recovery.cross_tenant",
        level=logging.WARNING,
        session_id=session_id,
        project_id=project_id,
        from_tenant=from_tenant,
        to_tenant=to_tenant,
        reason=reason
    )


def log_recovery_environment_reset(
    session_id: str,
    project_id: str,
    reason: str,
    old_environment_id: Optional[str] = None
) -> None:
    """Log environment reset recovery event."""
    log_event(
        "recovery.environment_reset",
        level=logging.WARNING,
        session_id=session_id,
        project_id=project_id,
        reason=reason,
        old_environment_id=old_environment_id
    )


def log_recovery_interaction_reset(
    session_id: str,
    project_id: str,
    reason: str,
    old_interaction_id: Optional[str] = None
) -> None:
    """Log interaction reset recovery event."""
    log_event(
        "recovery.interaction_reset",
        level=logging.INFO,
        session_id=session_id,
        project_id=project_id,
        reason=reason,
        old_interaction_id=old_interaction_id
    )


def log_recovery_workspace_sync(
    session_id: str,
    project_id: str,
    environment_id: str,
    trigger: str
) -> None:
    """Log workspace sync recovery event."""
    log_event(
        "recovery.workspace_sync",
        level=logging.INFO,
        session_id=session_id,
        project_id=project_id,
        environment_id=environment_id,
        trigger=trigger
    )


def log_sync_start(
    session_id: str,
    project_id: str,
    environment_id: str,
    file_count: int,
    total_size: int
) -> None:
    """Log sync start event."""
    log_event(
        "sync.start",
        session_id=session_id,
        project_id=project_id,
        environment_id=environment_id,
        file_count=file_count,
        total_size_bytes=total_size
    )


def log_sync_success(
    session_id: str,
    project_id: str,
    environment_id: str,
    synced_count: int,
    verified_count: int,
    duration_ms: Optional[int] = None
) -> None:
    """Log sync success event."""
    log_event(
        "sync.success",
        level=logging.INFO,
        duration_ms=duration_ms,
        session_id=session_id,
        project_id=project_id,
        environment_id=environment_id,
        synced_files=synced_count,
        verified_files=verified_count
    )


def log_sync_partial(
    session_id: str,
    project_id: str,
    environment_id: str,
    synced_count: int,
    failed_count: int,
    duration_ms: Optional[int] = None,
    reason: Optional[str] = None
) -> None:
    """Log partial sync event."""
    log_event(
        "sync.partial",
        level=logging.WARNING,
        duration_ms=duration_ms,
        session_id=session_id,
        project_id=project_id,
        environment_id=environment_id,
        synced_files=synced_count,
        failed_files=failed_count,
        reason=reason
    )


def log_sync_failure(
    session_id: str,
    project_id: str,
    environment_id: str,
    error: str,
    duration_ms: Optional[int] = None
) -> None:
    """Log sync failure event."""
    log_event(
        "sync.failure",
        level=logging.ERROR,
        duration_ms=duration_ms,
        session_id=session_id,
        project_id=project_id,
        environment_id=environment_id,
        error=redact_secrets(error)
    )


def log_storage_corrupted(
    storage_file: str,
    error: str
) -> None:
    """Log storage corruption event."""
    log_event(
        "storage.corrupted",
        level=logging.ERROR,
        storage_file=storage_file,
        error=error
    )


def log_storage_recovered(
    storage_file: str,
    recovery_method: str
) -> None:
    """Log storage recovery event."""
    log_event(
        "storage.recovered",
        level=logging.INFO,
        storage_file=storage_file,
        recovery_method=recovery_method
    )


# Health and Diagnostics

def check_health(
    config_path: Optional[str] = None,
    projects_path: Optional[str] = None,
    keys_path: Optional[str] = None,
    sessions_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Check health of all critical storage and configuration.
    
    Returns:
        Dictionary with health status of each component
    """
    from config import get_config_path, RouterConfig
    from registry import ProjectRegistry, DEFAULT_REGISTRY_PATH
    from keys import KeyPoolManager, DEFAULT_KEY_STATE_PATH
    from sessions import SessionStateManager, DEFAULT_SESSION_PATH
    from file_lock import FileAndThreadLock
    
    results = {}
    overall_healthy = True
    
    # Check config.json (optional - defaults used if missing)
    config_file = get_config_path(config_path)
    try:
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                json.load(f)
            results["config.json"] = "OK"
        else:
            results["config.json"] = "NOT_FOUND (using defaults)"
    except Exception as e:
        results["config.json"] = f"ERROR: {e}"
        overall_healthy = False
    
    # Check projects.json
    projects_file = projects_path or DEFAULT_REGISTRY_PATH
    try:
        if os.path.exists(projects_file):
            with open(projects_file, 'r') as f:
                json.load(f)
            results["projects.json"] = "OK"
        else:
            results["projects.json"] = "NOT_FOUND"
            overall_healthy = False
    except Exception as e:
        results["projects.json"] = f"ERROR: {e}"
        overall_healthy = False
     
    # Check key_states.json
    keys_file = keys_path or DEFAULT_KEY_STATE_PATH
    try:
        if os.path.exists(keys_file):
            with open(keys_file, 'r') as f:
                json.load(f)
            results["key_states.json"] = "OK"
        else:
            results["key_states.json"] = "NOT_FOUND"
            overall_healthy = False
    except Exception as e:
        results["key_states.json"] = f"ERROR: {e}"
        overall_healthy = False
     
    # Check sessions.json
    sessions_file = sessions_path or DEFAULT_SESSION_PATH
    try:
        if os.path.exists(sessions_file):
            with open(sessions_file, 'r') as f:
                json.load(f)
            results["sessions.json"] = "OK"
        else:
            results["sessions.json"] = "NOT_FOUND"
            overall_healthy = False
    except Exception as e:
        results["sessions.json"] = f"ERROR: {e}"
        overall_healthy = False
    
    # Check lock mechanism
    try:
        lock = FileAndThreadLock("/tmp/agy_health_check.lock")
        with lock:
            pass
        results["locks"] = "OK"
    except Exception as e:
        results["locks"] = f"ERROR: {e}"
        overall_healthy = False
    
    # Check key discovery
    try:
        pool = KeyPoolManager()
        discovered = pool.discover_keys()
        if discovered:
            results["key discovery"] = f"OK ({len(discovered)} keys)"
        else:
            results["key discovery"] = "NO_KEYS"
    except Exception as e:
        results["key discovery"] = f"ERROR: {e}"
        overall_healthy = False
    
    # Check configuration validity
    try:
        cfg = RouterConfig()
        results["configuration"] = "OK"
    except Exception as e:
        results["configuration"] = f"ERROR: {e}"
        overall_healthy = False
    
    return {
        "healthy": overall_healthy,
        "checks": results,
        "overall": "HEALTHY" if overall_healthy else "UNHEALTHY"
    }


def get_diagnostics(
    config_path: Optional[str] = None,
    projects_path: Optional[str] = None,
    keys_path: Optional[str] = None,
    sessions_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get comprehensive diagnostics of the router state.
    Read-only operation - does not modify any state files.
    
    Returns:
        Dictionary with diagnostics information
    """
    import subprocess
    from config import get_config_path, load_config
    from registry import ProjectRegistry, DEFAULT_REGISTRY_PATH
    from keys import KeyPoolManager, DEFAULT_KEY_STATE_PATH
    from sessions import SessionStateManager, DEFAULT_SESSION_PATH
    
    # Git info
    git_branch = "unknown"
    git_commit = "unknown"
    try:
        git_branch = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, cwd="/home/ubuntu/agy-router"
        ).stdout.strip() or "unknown"
        git_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd="/home/ubuntu/agy-router"
        ).stdout.strip() or "unknown"
    except Exception:
        pass
    
    # Load configs
    config = load_config(config_path)
    
    # Projects
    registry = ProjectRegistry(storage_path=projects_path or DEFAULT_REGISTRY_PATH)
    projects = registry.list_projects()
    
    # Sessions
    sess_mgr = SessionStateManager(storage_path=sessions_path or DEFAULT_SESSION_PATH)
    sessions = sess_mgr.list_sessions()
    
    session_counts = {"IDLE": 0, "ACTIVE": 0, "INVALIDATED": 0}
    for s in sessions:
        state = s.get("state", "IDLE")
        if state in session_counts:
            session_counts[state] += 1
    
    # Keys
    pool = KeyPoolManager(storage_path=keys_path or DEFAULT_KEY_STATE_PATH)
    key_status = pool.get_status()
    
    key_counts = {"ACTIVE": 0, "COOLDOWN": 0, "INACTIVE": 0}
    tenant_counts = {}
    for ref, entry in key_status.items():
        state = entry.get("state", "UNKNOWN")
        if state in key_counts:
            key_counts[state] += 1
        tenant = entry.get("tenant_id", "unknown")
        if tenant:
            tenant_counts[tenant] = tenant_counts.get(tenant, 0) + 1
    
    # Storage health
    storage_health = {}
    for name, path in [
        ("projects", projects_path or DEFAULT_REGISTRY_PATH),
        ("keys", keys_path or DEFAULT_KEY_STATE_PATH),
        ("sessions", sessions_path or DEFAULT_SESSION_PATH),
        ("config", get_config_path(config_path))
    ]:
        try:
            if os.path.exists(path):
                with open(path, 'r') as f:
                    json.load(f)
                storage_health[name] = "OK"
            else:
                storage_health[name] = "NOT_FOUND"
        except Exception as e:
            storage_health[name] = f"CORRUPTED: {e}"
    
    # Recent state
    last_session_update = 0
    last_key_update = 0
    for s in sessions:
        updated = s.get("updated_at", 0)
        if updated > last_session_update:
            last_session_update = updated
    
    for entry in key_status.values():
        used = entry.get("last_used_at", 0)
        if used > last_key_update:
            last_key_update = used
    
    return {
        "git": {
            "branch": git_branch,
            "commit": git_commit
        },
        "projects": {
            "registered": len(projects)
        },
        "sessions": session_counts,
        "keys": key_counts,
        "tenants": tenant_counts,
        "storage": storage_health,
        "config": {
            "default_key_index": config.default_key_index,
            "cooldown_seconds": config.cooldown_seconds,
            "max_total_sync_bytes": config.max_total_sync_bytes,
            "max_file_size_bytes": config.max_file_size_bytes,
            "log_level": config.log_level,
            "git_auto_checkpoint": config.git_auto_checkpoint,
            "default_session_timeout": config.default_session_timeout
        },
        "recent": {
            "last_session_updated": last_session_update if last_session_update else None,
            "last_key_used": last_key_update if last_key_update else None
        }
    }


def format_diagnostics_text(diagnostics: Dict[str, Any]) -> str:
    """Format diagnostics as human-readable text."""
    lines = []
    lines.append("=== agy-router Diagnostics ===")
    lines.append("")
    lines.append(f"Version/Commit:")
    lines.append(f"  Branch:   {diagnostics['git']['branch']}")
    lines.append(f"  Commit:   {diagnostics['git']['commit']}")
    lines.append("")
    lines.append("Projects:")
    lines.append(f"  Registered: {diagnostics['projects']['registered']}")
    lines.append("")
    lines.append("Sessions:")
    for state, count in diagnostics['sessions'].items():
        lines.append(f"  {state}: {count}")
    lines.append("")
    lines.append("Keys:")
    for state, count in diagnostics['keys'].items():
        lines.append(f"  {state}: {count}")
    lines.append("")
    lines.append("Tenants:")
    for tenant, count in diagnostics['tenants'].items():
        lines.append(f"  {tenant}: {count} keys")
    lines.append("")
    lines.append("Storage:")
    for name, status in diagnostics['storage'].items():
        lines.append(f"  {name}.json: {status}")
    lines.append("")
    lines.append("Configuration:")
    for key, value in diagnostics['config'].items():
        lines.append(f"  {key}: {value}")
    lines.append("")
    lines.append("Recent State:")
    recent = diagnostics.get('recent', {})
    if recent.get('last_session_updated'):
        lines.append(f"  Last Session Update: {datetime.fromtimestamp(recent['last_session_updated']).isoformat()}")
    if recent.get('last_key_used'):
        lines.append(f"  Last Key Used: {datetime.fromtimestamp(recent['last_key_used']).isoformat()}")
    lines.append("")
    lines.append("=============================")
    
    return "\n".join(lines)