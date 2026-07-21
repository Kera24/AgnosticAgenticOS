"""Canonical project specification: parses `project.md` (optional YAML
frontmatter + Markdown sections) into a normalised, versioned
ProjectSpecification -- the single input the Requirements Intelligence
Engine (Phase 3) consumes. See `.agentic/project/capability-intelligence-
design.md` Phase 1.
"""
from .parser import parse_project_spec
from .schema import CLASSIFICATIONS, FRONTMATTER_DEFAULTS, SECTION_SPECS
from .template import render_template

__all__ = ["parse_project_spec", "render_template", "SECTION_SPECS",
           "FRONTMATTER_DEFAULTS", "CLASSIFICATIONS"]
