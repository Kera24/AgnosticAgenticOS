PY ?= python
AGENTIC := $(PY) .agentic/run

.PHONY: agent-doctor agent-tick agent-queue agent-trust agent-audit \
        agent-goals agent-dry-run test

agent-doctor:      ## validate dependencies, config, providers, env vars
	$(AGENTIC) doctor

agent-tick:        ## run one complete workflow cycle
	$(AGENTIC) tick

agent-queue:       ## show tasks awaiting human review
	$(AGENTIC) queue

agent-trust:       ## render trust tiers and statistics
	$(AGENTIC) trust

agent-audit:       ## recent token and cost usage
	$(AGENTIC) audit

agent-goals:       ## evaluate standing goals
	$(AGENTIC) goals

agent-dry-run:     ## analyse + produce a work order without editing files
	$(AGENTIC) dry-run

test:              ## run the Agentic OS test suite (fully mocked, no API calls)
	$(PY) -m pytest tests -q
