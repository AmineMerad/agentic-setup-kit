# agents.mk — make targets for the agentic-setup-kit worktree agents + orchestrator container.
# Include from your Makefile:   -include .agent-kit/agents.mk
# Targets that need the orchestrator module (agents-image, agents-orchestrator*) only work after
# `install.py --with-orchestrator`.

AGENTCTL     := .agent-kit/agents/agentctl.sh
AGENTS_PROJ  := $(shell basename "$(CURDIR)" | tr 'A-Z ' 'a-z-')

.PHONY: agents-up agents-launch agents-list agents-log agents-stop \
        agents-image agents-orchestrator agents-orchestrator-logs agents-orchestrator-shell \
        agents-orchestrator-status agents-orchestrator-stop

agents-up: ## One headless agent in its own worktree — make agents-up FILE=task.txt NAME=name [BASE=ref] [WT=0]
	@test -n "$(FILE)" || (echo "usage: make agents-up FILE=task.txt NAME=name [BASE=main] [WT=0]" && exit 1)
	@test -f "$(FILE)" || (echo "FILE not found: $(FILE)" && exit 1)
	@BASE="$(BASE)" WT="$(WT)" $(AGENTCTL) up -f "$(FILE)" $(if $(NAME),-n $(NAME))

agents-launch: ## Launch by task id (tasks/<id>-task.txt) — make agents-launch ID=s-02 [BASE=main]
	@BASE="$(BASE)" $(AGENTCTL) launch $(ID) $(if $(BASE),-b $(BASE))

agents-list: ## List agents: pid, state, task, last output line
	@$(AGENTCTL) list

agents-log: ## Tail an agent log — make agents-log (latest) | NAME=name | ALL=1
	@$(AGENTCTL) log $(if $(NAME),-n $(NAME)) $(if $(ALL),-a) $(if $(LATEST),-l)

agents-stop: ## Stop agents — make agents-stop NAME=name (omit NAME = stop all)
	@$(AGENTCTL) stop $(if $(NAME),-n $(NAME))

# ─── Orchestrator container (scan tickets → launch/monitor/retry agents) ──────────────────
# Needs Docker/OrbStack and a gitignored .env.agents (GH_TOKEN + REPO_SLUG).
AGENTS_IMAGE ?= $(AGENTS_PROJ)-agent-orchestrator
AGENTS_NAME  ?= $(AGENTS_PROJ)-agent-orchestrator
AGENTS_CTX   := .agent-kit/orchestrator
AGENTS_ENV   := $(if $(wildcard .env.agents),--env-file .env.agents,)
AGENTS_PASS  := $(if $(SERIES),-e SERIES=$(SERIES),) $(if $(TICKETS),-e TICKETS=$(TICKETS),) \
                $(if $(MAX_PARALLEL),-e MAX_PARALLEL=$(MAX_PARALLEL),) \
                $(if $(MAX_ATTEMPTS),-e MAX_ATTEMPTS=$(MAX_ATTEMPTS),) \
                $(if $(filter 1,$(DRY_RUN)),-e DRY_RUN=1,)
# A dry run exits 0 after the scan; restarting it would loop forever, so only restart real runs.
AGENTS_RESTART := $(if $(filter 1,$(DRY_RUN)),,--restart unless-stopped)
# Extra docker flags, e.g. AGENTS_DOCKER_ARGS="-v mycache:/root/.cache/foo"
AGENTS_DOCKER_ARGS ?=

agents-image: ## Build the orchestrator image
	docker build -t $(AGENTS_IMAGE) -f $(AGENTS_CTX)/Dockerfile $(AGENTS_CTX)

agents-orchestrator: ## Start the orchestrator — SERIES=/TICKETS=/MAX_PARALLEL=/MAX_ATTEMPTS=/DRY_RUN=1
	@test -f .env.agents || (echo ".env.agents missing — copy .env.agents.example, set GH_TOKEN and REPO_SLUG" && exit 1)
	@docker rm -f $(AGENTS_NAME) >/dev/null 2>&1 || true
	docker run -d --name $(AGENTS_NAME) $(AGENTS_RESTART) \
	  $(AGENTS_ENV) $(AGENTS_PASS) $(AGENTS_DOCKER_ARGS) \
	  -v $(AGENTS_PROJ)-agents-workspace:/workspace \
	  -v $(AGENTS_PROJ)-agents-pnpm:/root/.local/share/pnpm \
	  -v $(AGENTS_PROJ)-agents-oc:/root/.local/share/opencode \
	  $(AGENTS_IMAGE)
	@echo "$(AGENTS_NAME) started — make agents-orchestrator-logs"

agents-orchestrator-logs: ## Follow the supervisor log
	docker logs -f $(AGENTS_NAME)
agents-orchestrator-shell: ## Shell into the container
	docker exec -it $(AGENTS_NAME) bash
agents-orchestrator-status: ## Agents + parked tickets inside the container
	docker exec -it $(AGENTS_NAME) bash -lc 'cd /workspace && bash .agent-kit/agents/agentctl.sh list; echo; python3 .agent-kit/orchestrator/state.py parked'
agents-orchestrator-stop: ## Stop and remove the container (volumes kept)
	docker rm -f $(AGENTS_NAME)
