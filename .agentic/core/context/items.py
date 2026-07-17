"""Context data structures.

ContextItem is one candidate piece of model input with provenance and a
trust level. ContextRequest describes what a call site needs. ContextPackage
is the budgeted, rendered result plus the audit trail of what was included,
omitted, and why.
"""
import datetime as _dt
import hashlib
import uuid

# section categories in stable render order (stable prefix first — this
# ordering is also the prompt-caching prefix contract, see ADR 0001/Phase 8)
CATEGORIES = [
    "policy",              # OS security policy / constitution (mandatory)
    "role_contract",       # role instructions (mandatory)
    "output_schema",       # required output format (mandatory)
    "project_summary",     # stable project architecture / summary
    "skill",               # selected skill instructions (untrusted)
    "work_order",          # current task / work order (mandatory)
    "code",                # retrieved code (untrusted)
    "memory",              # retrieved decisions/memories (untrusted)
    "knowledge",           # knowledge-vault sections (untrusted)
    "validation",          # deterministic-check failures (mandatory if present)
]

MANDATORY_CATEGORIES = {"policy", "role_contract", "output_schema",
                        "work_order", "validation"}

# sections that are never truncated, even at a boundary
NEVER_TRUNCATE = {"policy", "role_contract", "output_schema", "validation"}

TRUST_LEVELS = ("trusted", "untrusted")


def _now_iso():
    return _dt.datetime.now().isoformat(timespec="seconds")


def content_fingerprint(content):
    return hashlib.sha256((content or "").encode("utf-8",
                                                 "replace")).hexdigest()[:16]


class ContextItem:
    __slots__ = ("id", "category", "content", "source_type", "source_path",
                 "source_revision", "token_estimate", "relevance_score",
                 "trust_level", "created_at", "expires_at", "supersedes",
                 "metadata", "fingerprint")

    def __init__(self, category, content, source_type="os",
                 source_path=None, source_revision=None, relevance_score=0.5,
                 trust_level="trusted", created_at=None, expires_at=None,
                 supersedes=None, metadata=None, item_id=None):
        if category not in CATEGORIES:
            raise ValueError("unknown context category %r" % category)
        if trust_level not in TRUST_LEVELS:
            raise ValueError("unknown trust level %r" % trust_level)
        self.id = item_id or uuid.uuid4().hex[:12]
        self.category = category
        self.content = content or ""
        self.source_type = source_type
        self.source_path = source_path
        self.source_revision = source_revision
        self.token_estimate = None   # filled by the broker's tokenizer
        self.relevance_score = float(relevance_score)
        self.trust_level = trust_level
        self.created_at = created_at or _now_iso()
        self.expires_at = expires_at
        self.supersedes = supersedes
        self.metadata = metadata or {}
        self.fingerprint = content_fingerprint(self.content)

    def provenance(self):
        return {"id": self.id, "category": self.category,
                "source_type": self.source_type,
                "source_path": self.source_path,
                "source_revision": self.source_revision,
                "trust_level": self.trust_level,
                "token_estimate": self.token_estimate,
                "relevance_score": self.relevance_score}


class ContextRequest:
    __slots__ = ("project_id", "cycle_id", "task_id", "role", "backend",
                 "model", "requested_sections", "maximum_input_tokens",
                 "reserved_output_tokens", "retrieval_query",
                 "relevant_paths", "validation_failures",
                 "skill_requirements")

    def __init__(self, role, project_id=None, cycle_id=None, task_id=None,
                 backend=None, model=None, requested_sections=None,
                 maximum_input_tokens=None, reserved_output_tokens=None,
                 retrieval_query=None, relevant_paths=None,
                 validation_failures=None, skill_requirements=None):
        self.role = role
        self.project_id = project_id
        self.cycle_id = cycle_id
        self.task_id = task_id
        self.backend = backend
        self.model = model
        self.requested_sections = requested_sections
        self.maximum_input_tokens = maximum_input_tokens
        self.reserved_output_tokens = reserved_output_tokens
        self.retrieval_query = retrieval_query
        self.relevant_paths = relevant_paths or []
        self.validation_failures = validation_failures or []
        self.skill_requirements = skill_requirements or []


class ContextPackage:
    def __init__(self, package_id, role, token_budget):
        self.package_id = package_id
        self.role = role
        self.token_budget = token_budget       # input budget after reserve
        self.token_estimate = 0
        self.reserved_output_tokens = 0
        self.sections = {c: [] for c in CATEGORIES}   # category -> items
        self.omitted_items = []                # (provenance, reason)
        self.included_reasons = {}             # item id -> reason
        self.rendered = None                   # final prompt string
        self.stable_prefix_chars = 0           # length of the cacheable prefix
        self.candidate_total_tokens = 0        # all candidates pre-selection
        self.created_at = _now_iso()

    def items(self):
        for category in CATEGORIES:
            for item in self.sections[category]:
                yield item

    def provenance(self):
        return [i.provenance() for i in self.items()]

    def summary(self):
        """Ledger-safe summary: ids, categories, sizes, reasons — no
        content bodies, so no secrets."""
        tokens_by_category = {
            c: sum(i.token_estimate or 0 for i in self.sections[c])
            for c in CATEGORIES if self.sections[c]}
        omitted_tokens = sum((p or {}).get("token_estimate") or 0
                             for p, _r in self.omitted_items)
        return {
            "package_id": self.package_id, "role": self.role,
            "created_at": self.created_at,
            "token_budget": self.token_budget,
            "token_estimate": self.token_estimate,
            "reserved_output_tokens": self.reserved_output_tokens,
            "stable_prefix_chars": self.stable_prefix_chars,
            "tokens_by_category": tokens_by_category,
            "omitted_tokens": omitted_tokens,
            "candidate_total_tokens": self.candidate_total_tokens,
            "estimated_savings_tokens": max(
                0, self.candidate_total_tokens - self.token_estimate),
            "measurement": "estimated",   # local estimates, never provider-reported
            "included": [dict(p, reason=self.included_reasons.get(p["id"], ""))
                         for p in self.provenance()],
            "omitted": [{"item": p, "reason": r}
                        for p, r in self.omitted_items],
        }
