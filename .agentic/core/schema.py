"""Minimal local JSON-Schema validator (subset). We validate every model
response locally instead of trusting provider 'JSON modes'.

Supported keywords: type (str or list; object/array/string/number/integer/
boolean/null), enum, required, properties, items, minimum, maximum,
minLength, maxLength, pattern. Unknown keywords are ignored.
"""
import json
import re

_TYPES = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _type_ok(value, tname):
    py = _TYPES.get(tname)
    if py is None:
        return True
    if isinstance(value, bool) and tname in ("integer", "number"):
        return False
    return isinstance(value, py)


def validate(instance, schema, path="$", errors=None):
    """Return a list of human-readable violations. Empty list == valid."""
    if errors is None:
        errors = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(instance, x) for x in types):
            errors.append("%s: expected type %s, got %s"
                          % (path, "/".join(types), type(instance).__name__))
            return errors
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("%s: %r not in enum %r" % (path, instance, schema["enum"]))
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append("%s: shorter than minLength %d" % (path, schema["minLength"]))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append("%s: longer than maxLength %d" % (path, schema["maxLength"]))
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append("%s: does not match pattern %s" % (path, schema["pattern"]))
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append("%s: %s below minimum %s" % (path, instance, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append("%s: %s above maximum %s" % (path, instance, schema["maximum"]))
    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errors.append("%s: missing required property %r" % (path, req))
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                validate(instance[key], sub, "%s.%s" % (path, key), errors)
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            validate(item, schema["items"], "%s[%d]" % (path, i), errors)
    return errors


def load_schema(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
