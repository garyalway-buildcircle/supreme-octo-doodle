.PHONY: help jira

PYTHON ?= python3
OUT ?= out/jira

.DEFAULT_GOAL := help

ifneq (,$(wildcard .env))
include .env
export
endif

help:
	@printf '%s\n' \
		'make jira PROJECT=PROJ START=100' \
		'make jira PROJECT=PROJ START=100 END=120' \
		'Reads JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN from the environment or .env.'

jira:
	@test -n "$(PROJECT)" || { echo "PROJECT is required. Example: make jira PROJECT=PROJ START=100" >&2; exit 1; }
	@test -n "$(START)" || { echo "START is required." >&2; exit 1; }
	$(PYTHON) -m tools.jira --project "$(PROJECT)" --start "$(START)" $(if $(END),--end "$(END)") --out "$(OUT)"
