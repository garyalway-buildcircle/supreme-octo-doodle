.PHONY: help jira confluence links git

PYTHON ?= python3
OUT ?= out/jira
CONFLUENCE_OUT ?= out/confluence
GIT_OUT ?= out/git

.DEFAULT_GOAL := help

ifneq (,$(wildcard .env))
include .env
export
endif

help:
	@printf '%s\n' \
		'make jira PROJECT=PROJ START=100' \
		'make jira PROJECT=PROJ START=100 END=120' \
		'make confluence PAGE=123456' \
		'make links' \
		'make git URL=https://github.com/org/name' \
		'make git REPOS=repos.txt' \
		'Reads JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN from the environment or .env.' \
		'Reads GIT_TOKEN from the environment or .env when a GitHub repository is private.'

jira:
	@test -n "$(PROJECT)" || { echo "PROJECT is required. Example: make jira PROJECT=PROJ START=100" >&2; exit 1; }
	@test -n "$(START)" || { echo "START is required." >&2; exit 1; }
	$(PYTHON) -m tools.jira --project "$(PROJECT)" --start "$(START)" $(if $(END),--end "$(END)") --out "$(OUT)"
	$(PYTHON) -m tools.links --jira "$(OUT)" --confluence "$(CONFLUENCE_OUT)"

confluence:
	@test -n "$(PAGE)" || { echo "PAGE is required. Example: make confluence PAGE=123456" >&2; exit 1; }
	$(PYTHON) -m tools.confluence --page "$(PAGE)" --out "$(CONFLUENCE_OUT)"
	$(PYTHON) -m tools.links --jira "$(OUT)" --confluence "$(CONFLUENCE_OUT)"

links:
	$(PYTHON) -m tools.links --jira "$(OUT)" --confluence "$(CONFLUENCE_OUT)"

git:
	@test -n "$(URL)$(REPOS)" || { echo "URL or REPOS is required. Example: make git URL=https://github.com/org/name" >&2; exit 1; }
	@test -z "$(URL)" || test -z "$(REPOS)" || { echo "Pass URL or REPOS, not both." >&2; exit 1; }
	$(PYTHON) -m tools.git $(if $(URL),--url "$(URL)") $(if $(REPOS),--repos "$(REPOS)") --out "$(GIT_OUT)"
