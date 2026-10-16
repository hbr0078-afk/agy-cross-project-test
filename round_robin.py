"""
Phase 8-2 (A): request-level round-robin across distinct Google Cloud
projects/tenants, plus the per-request local-workspace flow (B/C).

Rotation is keyed on distinct PROJECTS (tenant ids), never on raw key
references: several keys inside one project are a single quota unit. The
pointer lives in a JSON file guarded by the same file/thread lock used
everywhere else, so it is safe across threads AND processes.
"""
import os
import json
import tempfile
from typing import Any, Dict, List, Optional

from file_lock import FileAndThreadLock
from keys import AllKeysExhaustedError, get_api_key_by_index
from observability import log_event

DEFAULT_RR_STATE_PATH = os.path.join(os.path.expanduser("~/.agy-router"), "rr_state.json")

# Keys with no project identity are grouped into a SINGLE slot. Several keys from
# the same (unknown) project must never inflate the number of quota units; the
# cost of guessing wrong is only that we under-use keys until real tenant metadata
# (AGY_TENANT_<n>) is supplied, never that we over-count a project's quota.
UNKNOWN_PROJECT_ID = "__unknown__"


class RoundRobinSelector:
    """Advances a persisted project pointer once per top-level request."""

    def __init__(self, key_pool: Any, storage_path: Optional[str] = None,
                 disabled_projects: Optional[List[str]] = None):
        if key_pool is None:
            raise ValueError("RoundRobinSelector requires a key_pool.")
        self.key_pool = key_pool
        self.storage_path = os.path.abspath(storage_path or DEFAULT_RR_STATE_PATH)
        self.storage_dir = os.path.dirname(self.storage_path)
        self.disabled_projects = set(disabled_projects or [])
        self._lock = FileAndThreadLock(self.storage_path + ".lock")
        if self.storage_dir:
            os.makedirs(self.storage_dir, mode=0o700, exist_ok=True)

    # ---- persisted pointer ----------------------------------------------
    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.storage_path):
            return {}
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            # Corrupt pointer must not wedge routing: start over cleanly.
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        if self.storage_dir:
            os.makedirs(self.storage_dir, mode=0o700, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self.storage_dir, delete=False,
                                         encoding="utf-8") as tf:
            json.dump(data, tf, indent=2, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
            tmp = tf.name
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.storage_path)

    # ---- project view ----------------------------------------------------
    def eligible_projects(self) -> List[Dict[str, Any]]:
        """Distinct projects with >=1 usable key, in deterministic order.

        Keys sharing a tenant_id are one quota unit. Keys with no tenant_id are
        collapsed into the single ``UNKNOWN_PROJECT_ID`` slot rather than being
        counted as if each key were its own project.
        """
        status = self.key_pool.get_status()
        groups: Dict[str, Dict[str, Any]] = {}
        for ref in sorted(status, key=lambda r: status[r].get("index", 0)):
            entry = status[ref]
            tenant = entry.get("tenant_id") or ""
            project_id = tenant if tenant else UNKNOWN_PROJECT_ID
            groups.setdefault(project_id, {"project_id": project_id, "tenant_id": tenant,
                                           "key_refs": []})
            groups[project_id]["key_refs"].append(ref)

        projects: List[Dict[str, Any]] = []
        for project_id in sorted(groups):
            if project_id in self.disabled_projects:
                continue
            group = groups[project_id]
            usable = [r for r in group["key_refs"]
                      if status[r].get("state") == "ACTIVE"
                      and get_api_key_by_index(status[r].get("index", 0))]
            if not usable:
                continue  # cooldown / inactive / missing credentials
            projects.append({"project_id": project_id, "tenant_id": group["tenant_id"],
                             "key_refs": usable})
        return projects

    def next_project(self) -> Dict[str, Any]:
        """Pick the next project after the last one and persist the pointer."""
        def _tx() -> Dict[str, Any]:
            projects = self.eligible_projects()
            if not projects:
                raise AllKeysExhaustedError("No eligible projects for round-robin routing.")
            order = [p["project_id"] for p in projects]
            data = self._load()
            last = data.get("last_project")
            idx = order.index(last) if last in order else -1
            chosen = projects[(idx + 1) % len(projects)]
            data["last_project"] = chosen["project_id"]
            counts = data.setdefault("counts", {})
            entry = counts.setdefault(chosen["project_id"],
                                      {"requests": 0, "tokens": 0, "tokens_estimated": False})
            entry["requests"] = entry.get("requests", 0) + 1
            self._save(data)
            log_event("rr.project_selected", project_id=chosen["project_id"], rotation_order=order)
            return chosen

        return self._lock.run(_tx)

    def record_usage(self, project_id: str, usage: Optional[Any] = None,
                     prompt_chars: int = 0, output_chars: int = 0) -> Dict[str, Any]:
        """Accumulate token usage; fall back to a labelled char/4 estimate."""
        tokens = None
        estimated = False
        if isinstance(usage, dict):
            for key in ("total_token_count", "totalTokenCount", "total_tokens"):
                val = usage.get(key)
                if isinstance(val, int) and val >= 0:
                    tokens = val
                    break
            if tokens is None:
                p_in = usage.get("prompt_token_count") or usage.get("input_tokens")
                c_out = usage.get("candidates_token_count") or usage.get("output_tokens")
                if isinstance(p_in, int) and isinstance(c_out, int):
                    tokens = p_in + c_out
        if tokens is None:
            tokens = max(0, prompt_chars + output_chars) // 4
            estimated = True

        def _tx() -> Dict[str, Any]:
            data = self._load()
            entry = data.setdefault("counts", {}).setdefault(
                project_id, {"requests": 0, "tokens": 0, "tokens_estimated": False})
            entry["tokens"] = entry.get("tokens", 0) + tokens
            if estimated:
                entry["tokens_estimated"] = True
            self._save(data)
            return {"project_id": project_id, "tokens_added": tokens, "estimated": estimated}

        return self._lock.run(_tx)

    def get_counts(self) -> Dict[str, Any]:
        return self._load().get("counts", {})

    def reset(self) -> None:
        def _tx() -> None:
            self._save({})
        self._lock.run(_tx)


class RequestRoundRobinRunner:
    """Per-request flow: select project -> collect local state -> call -> validate -> apply."""

    def __init__(self, client: Any, workspace: Any, selector: Optional[RoundRobinSelector] = None):
        self.client = client
        self.workspace = workspace
        self.selector = selector or getattr(client, "round_robin", None)

    def build_input(self, prompt: str, context: Dict[str, Any]) -> str:
        parts: List[str] = []
        if context.get("task_context"):
            parts.append(str(context["task_context"]))
        parts.append(prompt)
        prev = context.get("prev_task_state")
        if prev:
            parts.append("Previous task state:\n" + json.dumps(prev, ensure_ascii=False, sort_keys=True))
        for rel, content in context.get("files", {}).items():
            parts.append(f"File: {rel}\n```\n{content}\n```")
        return "\n\n".join(parts)

    def run_request(self, prompt: str, required_files: Optional[List[str]] = None,
                    task_context: Optional[str] = None, timeout: int = 120) -> Dict[str, Any]:
        from local_workspace import WorkspaceError, file_sha256

        context = self.workspace.collect_context(required_files, task_context)
        input_text = self.build_input(prompt, context)
        result = self.client.create_interaction(prompt=input_text, timeout=timeout)
        outcome = {"result": result, "input": input_text, "context": context, "applied": []}
        if not result.get("success"):
            return outcome
        try:
            changes = self.workspace.parse_changes(result.get("output", ""))
        except WorkspaceError as e:
            # Malformed / invalid output -> leave every existing file untouched.
            self.workspace.save_checkpoint(
                goal=prompt, next_todo=prompt,
                test_results={"validated": False, "error": str(e)})
            outcome["validation_error"] = str(e)
            return outcome
        applied = self.workspace.apply_changes(changes)
        state = {rel: {"sha256": file_sha256(os.path.join(self.workspace.root, rel))}
                 for rel in applied}
        self.workspace.save_checkpoint(
            goal=prompt, next_todo=None, changed_files=applied,
            test_results={"validated": True},
            last_good_state={"applied": applied, "files": state})
        outcome["applied"] = applied
        return outcome


def selector_from_config(config: Any, key_pool: Any,
                         storage_path: Optional[str] = None) -> Optional[RoundRobinSelector]:
    """Build a selector only when the feature is explicitly enabled."""
    if not getattr(config, "request_round_robin", False):
        return None
    return RoundRobinSelector(key_pool=key_pool, storage_path=storage_path)
