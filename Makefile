PY ?= python
AGENTIC := $(PY) .agentic/run
PLAN ?= plan.md

.PHONY: agent-setup agent-doctor agent-tick agent-queue agent-trust \
        agent-audit agent-goals agent-dry-run agent-capacity agent-backends \
        agent-test project-start project-run project-resume project-status \
        project-pause project-review package test

# --- setup & diagnostics ------------------------------------------------------
agent-setup:       ## interactive machine configuration (writes config.machine.yaml)
	$(AGENTIC) setup

agent-doctor:      ## validate deps, config, backends, auth status, scheduler
	$(AGENTIC) doctor

agent-capacity:    ## capacity ledger + next-cycle estimate (local estimates)
	$(AGENTIC) capacity

agent-backends:    ## backend circuit-breaker states
	$(AGENTIC) backends

# --- full-project build --------------------------------------------------------
project-start:     ## convert a plan into architecture/backlog: make project-start PLAN=plan.md
	$(AGENTIC) project-start $(PLAN)

project-run:       ## run the next eligible cycle(s)
	$(AGENTIC) project-run

project-resume:    ## resume after pause/restart and run if eligible
	$(AGENTIC) project-resume

project-status:    ## scheduler + progress + blockers
	$(AGENTIC) project-status

project-pause:     ## pause autonomous cycles
	$(AGENTIC) project-pause

project-review:    ## run the final project audit now
	$(AGENTIC) project-review

# --- repository maintenance mode -------------------------------------------------
agent-tick:        ## one maintenance workflow cycle
	$(AGENTIC) tick

agent-dry-run:     ## analysis + work order without editing files
	$(AGENTIC) dry-run

agent-queue:       ## tasks awaiting human review
	$(AGENTIC) queue

agent-trust:       ## trust tiers and statistics
	$(AGENTIC) trust

agent-audit:       ## recent API token and cost usage
	$(AGENTIC) audit

agent-goals:       ## evaluate standing goals
	$(AGENTIC) goals

# --- tests & packaging -------------------------------------------------------------
agent-test test:   ## full mocked test suite (no network, no quota consumed)
	$(PY) -m pytest tests -q

package:           ## clean distributable archive (excludes local artefacts)
	$(AGENTIC) package
