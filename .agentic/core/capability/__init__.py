"""Capability intelligence package (Phase 2+): taxonomy, requirements
inference, the capability graph, and the resolver. See
`.agentic/project/capability-intelligence-design.md`."""
from .taxonomy import Taxonomy, TaxonomyError, load_taxonomy

__all__ = ["Taxonomy", "TaxonomyError", "load_taxonomy"]
