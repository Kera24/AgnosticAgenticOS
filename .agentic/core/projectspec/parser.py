"""Pure, deterministic parser: `project.md` text -> normalised
ProjectSpecification dict. No I/O, no model calls -- safe to unit test
with raw strings. Never crashes on malformed input; malformed input
becomes a warning plus a graceful fallback (the whole document treated as
unstructured content), so an existing `plan.md` with no frontmatter and
no recognised headers still parses into a usable specification -- this is
exactly how `plan.md` backward compatibility is preserved (see
Requirement 9 in Phase 1)."""
import re

import yaml

from .schema import (FRONTMATTER_DEFAULTS, SCHEMA_VERSION, SECTION_SPECS)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n?", re.DOTALL)
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

_SECTION_LOOKUP = {name.strip().lower(): name for name in SECTION_SPECS}


def _has_real_content(content):
    """A section counts as "present" only once something beyond an HTML
    hint comment is written -- `project template` fills every section
    with a `<!-- ... -->` hint, and that must still parse as absent."""
    return bool(_HTML_COMMENT_RE.sub("", content).strip())


def _normalise_newlines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _parse_frontmatter(text):
    """Returns (frontmatter_dict, remaining_body, warnings). Frontmatter
    is entirely optional; a `---` block that isn't valid YAML is reported
    as a warning and treated as absent (body still starts after the
    fence so it isn't swallowed into Product Vision)."""
    warnings = []
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text, warnings
    block = match.group(1)
    body = text[match.end():]
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        warnings.append("frontmatter: invalid YAML (%s); ignored, "
                        "platform defaults used" % str(exc)[:150])
        return {}, body, warnings
    if data is None:
        return {}, body, warnings
    if not isinstance(data, dict):
        warnings.append("frontmatter: expected a mapping, got %s; "
                        "ignored, platform defaults used"
                        % type(data).__name__)
        return {}, body, warnings
    return data, body, warnings


def _resolve_frontmatter(raw):
    """Fill every field from FRONTMATTER_DEFAULTS; invalid values fall
    back to the default with a warning rather than propagating an
    unvalidated value."""
    resolved, warnings = {}, []
    for field, (default, allowed) in FRONTMATTER_DEFAULTS.items():
        if field in raw:
            value = raw[field]
            if allowed is not None and value not in allowed:
                warnings.append(
                    "frontmatter.%s: %r is not one of %r; using default %r"
                    % (field, value, list(allowed), default))
                value = default
        else:
            value = default
        resolved[field] = value
    # schema_version is always the CURRENT parser version once resolved;
    # the raw user-supplied value (if any) is preserved for migration.
    resolved["agentic_project_version"] = SCHEMA_VERSION
    for key in raw:
        if key not in FRONTMATTER_DEFAULTS:
            warnings.append("frontmatter: unrecognised field %r ignored"
                            % key)
    return resolved, warnings


def _split_sections(body, line_offset):
    """Returns (sections: {canonical_name: [(content, start, end), ...]},
    extra: {raw_heading: content}, in document order). Every heading
    occurrence is captured so duplicates can be detected and reported."""
    lines = body.split("\n")
    headings = []   # (canonical_name_or_None, raw_heading, line_index)
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            raw_heading = m.group(1).strip()
            canonical = _SECTION_LOOKUP.get(raw_heading.lower())
            headings.append((canonical, raw_heading, i))

    sections, extra = {}, {}
    for idx, (canonical, raw_heading, start) in enumerate(headings):
        end = headings[idx + 1][2] if idx + 1 < len(headings) else len(lines)
        content = "\n".join(lines[start + 1:end]).strip()
        start_line, end_line = line_offset + start + 1, line_offset + end
        if canonical:
            sections.setdefault(canonical, []).append(
                (content, start_line, end_line))
        else:
            extra.setdefault(raw_heading, []).append(
                (content, start_line, end_line))
    return sections, extra


def parse_project_spec(text):
    """The single entry point. Returns a plain-dict ProjectSpecification:

    schema_version, frontmatter, sections, assumptions,
    blocking_questions, warnings, extra_sections, raw_text.
    """
    text = _normalise_newlines(text or "")
    frontmatter_raw, body, fm_warnings = _parse_frontmatter(text)
    frontmatter, resolve_warnings = _resolve_frontmatter(frontmatter_raw)
    warnings = fm_warnings + resolve_warnings

    # line offset: how many lines the frontmatter block consumed, so
    # source references point at the ORIGINAL file, not the stripped body
    offset = text[:len(text) - len(body)].count("\n") if body != text else 0
    found, extra_raw = _split_sections(body, offset)

    sections, assumptions, blocking_questions = {}, [], []
    for name, spec in SECTION_SPECS.items():
        occurrences = found.get(name, [])
        if len(occurrences) > 1:
            warnings.append(
                "duplicate section %r: using the first occurrence, "
                "ignoring %d later occurrence(s)"
                % (name, len(occurrences) - 1))
        content, start_line, end_line = (occurrences[0] if occurrences
                                         else ("", None, None))
        present = _has_real_content(content)
        sections[name] = {
            "present": present, "content": content,
            "start_line": start_line, "end_line": end_line,
        }
        if not present:
            classification = spec["classification"]
            if classification == "materially_blocking":
                blocking_questions.append({
                    "section": name, "classification": classification,
                    "question": spec["question"],
                })
            else:
                assumptions.append({
                    "section": name, "classification": classification,
                    "value": spec.get("default", ""),
                    "reason": "section absent or empty in project.md",
                })

    extra_sections = {
        heading: "\n".join(c for c, _s, _e in occ).strip()
        for heading, occ in extra_raw.items()
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "frontmatter": frontmatter,
        "sections": sections,
        "assumptions": assumptions,
        "blocking_questions": blocking_questions,
        "warnings": warnings,
        "extra_sections": extra_sections,
        "raw_text": text,
    }
