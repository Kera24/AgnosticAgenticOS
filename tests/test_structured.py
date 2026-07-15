"""Malformed JSON handling, schema validation, schema-repair retry."""
import json

from conftest import AGENTIC_SRC, Transport, oai_body
from core.invoke import invoke_model
from core.jsonx import extract_first_json
from core.schema import load_schema, validate

TRIAGE_SCHEMA = load_schema(str(AGENTIC_SRC / "schemas" / "triage.schema.json"))
ORDER_SCHEMA = load_schema(str(AGENTIC_SRC / "schemas" / "work-order.schema.json"))

QUIET = '{"status": "quiet", "findings": []}'


# 6. schema validation ---------------------------------------------------------
def test_schema_accepts_valid_and_rejects_invalid():
    assert validate(json.loads(QUIET), TRIAGE_SCHEMA) == []
    assert validate({"status": "quiet"}, TRIAGE_SCHEMA)          # missing findings
    assert validate({"status": "loud", "findings": []}, TRIAGE_SCHEMA)  # bad enum
    bad_conf = {"status": "findings", "findings": [
        {"finding": "x", "evidence": [], "status": "actionable",
         "contract_sensitive": False, "confidence": 3.0}]}
    assert any("maximum" in v for v in validate(bad_conf, TRIAGE_SCHEMA))
    assert any("pattern" in v for v in validate(
        {"action": "execute", "item": "i", "skill": "Not Kebab", "spec": "s",
         "done_when": [], "allowed_paths": [], "forbidden_paths": [],
         "maximum_changed_lines": 1, "risk": "low", "queue_reason": None},
        ORDER_SCHEMA))


def test_extract_json_from_prose_and_fences():
    text = "Sure! Here is the result:\n```json\n" + QUIET + "\n```\nDone."
    assert extract_first_json(text) == {"status": "quiet", "findings": []}
    assert extract_first_json("no json here") is None
    assert extract_first_json("{broken} " + QUIET) == {"status": "quiet",
                                                       "findings": []}


# 5. malformed JSON handling (+ repair retry) -----------------------------------
def test_malformed_then_repaired(base_cfg, budget):
    transport = Transport([(200, oai_body("here you go, no json")),
                           (200, oai_body(QUIET))])
    resp = invoke_model(base_cfg, "triage", "scan", budget=budget,
                        transport=transport, output_schema=TRIAGE_SCHEMA,
                        sleeper=lambda s: None)
    assert resp["ok"] is True
    assert resp["structured_output"]["status"] == "quiet"
    assert len(transport.calls) == 2
    assert "SCHEMA REPAIR" in transport.calls[1]["body"]["messages"][-1]["content"]


def test_malformed_twice_fails_typed(base_cfg, budget):
    transport = Transport([(200, oai_body("nope")), (200, oai_body("still nope"))])
    resp = invoke_model(base_cfg, "triage", "scan", budget=budget,
                        transport=transport, output_schema=TRIAGE_SCHEMA,
                        sleeper=lambda s: None)
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "malformed_output"
    assert len(transport.calls) == 2      # exactly one repair retry, no loop
