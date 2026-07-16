"""Bridge from the orchestration call sites to the Context Broker.

compose() converts a legacy (role_prompt, input_data, schema) call into
ContextItems, classifies each input key into a section with the right trust
level, and returns the built ContextPackage. This is the single funnel: both
the maintenance tick and the project build assemble ALL model input here.
"""
import json

from .. import config as config_mod
from .broker import ContextBroker, schema_item
from .items import ContextItem, ContextRequest
from .ledger import ledger_appender

# input_data keys -> (category, trust). Repository-derived text is untrusted
# data by definition; OS-internal structures are trusted.
KEY_CLASSIFICATION = {
    "repository": ("code", "untrusted"),
    "repository_files": ("code", "untrusted"),
    "diff": ("code", "untrusted"),
    "current_diff": ("code", "untrusted"),
    "open_issues": ("code", "untrusted"),
    "ci_results": ("code", "untrusted"),
    "recent_commits": ("code", "untrusted"),
    "status": ("code", "untrusted"),
    "changed_files": ("code", "untrusted"),
    "failing_checks": ("validation", "trusted"),
    "scope_violations": ("validation", "trusted"),
    "deterministic_checks": ("validation", "trusted"),
    "safe_command_results": ("validation", "trusted"),
    "qa_findings": ("validation", "untrusted"),
    "goal_violations": ("validation", "trusted"),
    "memories": ("memory", "untrusted"),
    "knowledge": ("knowledge", "untrusted"),
    "skills": ("skill", "untrusted"),
    "architecture": ("project_summary", "trusted"),
    "progress": ("project_summary", "trusted"),
}

_RELEVANCE = {"work_order": 1.0, "validation": 0.95, "project_summary": 0.7,
              "code": 0.6, "memory": 0.5, "knowledge": 0.45, "skill": 0.55}

_SHARED_POLICY_FILES = ("shared-autonomy.md", "shared-scope.md")


def _policy_text():
    base = config_mod.AGENTIC_DIR / "prompts"
    parts = []
    for name in _SHARED_POLICY_FILES:
        path = base / name
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def _as_text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def input_items(input_data):
    """Convert an input_data mapping (or raw string) into ContextItems."""
    items = []
    if input_data is None:
        return items
    if isinstance(input_data, str):
        return [ContextItem("work_order", input_data, source_type="os",
                            relevance_score=1.0)]
    for key, value in input_data.items():
        category, trust = KEY_CLASSIFICATION.get(key, ("work_order",
                                                       "trusted"))
        if key == "repository" and isinstance(value, dict):
            # split the snapshot: file list + one item per file so the
            # broker can budget, dedupe, and omit per file
            listing = value.get("file_list")
            if listing:
                items.append(ContextItem(
                    "code", "Repository files:\n" + "\n".join(listing),
                    source_type="repository", source_path="(file list)",
                    relevance_score=0.4, trust_level="untrusted"))
            for path, content in (value.get("files") or {}).items():
                items.append(ContextItem(
                    "code", "```%s\n%s\n```" % (path, content),
                    source_type="repository", source_path=path,
                    relevance_score=0.65, trust_level="untrusted"))
            continue
        text = "%s:\n%s" % (key, _as_text(value))
        items.append(ContextItem(
            category, text,
            source_type="repository" if trust == "untrusted" else "os",
            source_path=key if trust == "untrusted" else None,
            relevance_score=_RELEVANCE.get(category, 0.5),
            trust_level=trust))
    return items


def compose(cfg, role, role_prompt, input_data=None, schema=None, *,
            memory_dir, backend=None, model=None, task_id=None,
            cycle_id=None, extra_items=None, max_input_tokens=None,
            reserved_output_tokens=None):
    """Build the ContextPackage for one model invocation."""
    items = [
        ContextItem("policy", _policy_text(), source_type="os",
                    relevance_score=1.0),
        ContextItem("role_contract", role_prompt, source_type="os",
                    relevance_score=1.0),
    ]
    if schema is not None:
        items.append(schema_item(schema))
    items.extend(input_items(input_data))
    items.extend(extra_items or [])
    request = ContextRequest(
        role=role, backend=backend, model=model, task_id=task_id,
        cycle_id=cycle_id, maximum_input_tokens=max_input_tokens,
        reserved_output_tokens=reserved_output_tokens)
    broker = ContextBroker(cfg, ledger_writer=ledger_appender(memory_dir))
    return broker.build(request, items)
