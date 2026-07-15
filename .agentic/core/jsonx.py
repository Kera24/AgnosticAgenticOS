"""Extract structured output from plain model text. We never depend on a
provider JSON mode: we ask for JSON, then pull the first parseable object
out of whatever came back (prose, code fences, etc.)."""
import json


def extract_first_json(text):
    """Return the first JSON object found in text, or None."""
    if not isinstance(text, str):
        return None
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _end = decoder.raw_decode(text, idx)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        idx = text.find("{", idx + 1)
    return None
