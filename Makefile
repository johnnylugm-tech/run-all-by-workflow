# taskq — project-level verification targets
#
# `make verify-system` is the canonical execute_verification_target
# for gate2/3/4 evaluation: it boots a fresh subprocess, runs all
# unit + integration tests, and exercises the CLI end-to-end against
# a real on-disk tasks.json. Any of these steps failing → make exits
# non-zero → scorer returns 0 (binary pass/fail per registry).
#
# Override interpreter:  make PYTHON=/path/to/python verify-system

PYTHON     ?= python3
PYTHONPATH := 03-development/src

.PHONY: verify-system verify-cli verify-tests help

help:
	@echo "Targets:"
	@echo "  verify-system  - full end-to-end verification (gate2/3/4 target)"
	@echo "  verify-tests   - run unit + integration test suites"
	@echo "  verify-cli     - smoke-test the CLI end-to-end as a real subprocess"

verify-tests:
	@COVERAGE_PROCESS_START=$(PWD)/.coveragerc $(PYTHON) -m coverage run \
		--rcfile=.coveragerc -m pytest 03-development/tests -q --tb=short --no-header
	@$(PYTHON) -m coverage combine > /dev/null 2>&1 || true
	@$(PYTHON) -m coverage report --skip-covered --include='03-development/src/*' > /dev/null

verify-cli:
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m taskq --help > /dev/null
	@PYTHONPATH=$(PYTHONPATH) TASKQ_HOME=$$(mktemp -d) $(PYTHON) -m taskq submit 'echo hello' > /dev/null
	@PYTHONPATH=$(PYTHONPATH) TASKQ_HOME=$$(mktemp -d) $(PYTHON) -m taskq list > /dev/null
	@PYTHONPATH=$(PYTHONPATH) TASKQ_HOME=$$(mktemp -d) $(PYTHON) -m taskq clear > /dev/null

verify-system: verify-tests verify-cli
	@echo "verify-system: PASS"
