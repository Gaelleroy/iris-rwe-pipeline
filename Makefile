# IRIS RWE pipeline
.PHONY: help setup data ingest validate transform cohort analyze pipeline test lint clean aws-up

CONFIG ?= config/study.yaml
PY     ?= python3

help:
	@echo "make setup     install python dependencies"
	@echo "make pipeline  run every stage end to end"
	@echo "make data      regenerate synthetic EHR with injected defects"
	@echo "make validate  run structural + clinical QC only"
	@echo "make cohort    rebuild the analytic cohort"
	@echo "make analyze   run the Python analysis (R: Rscript r/analysis.R)"
	@echo "make test      run the test suite"
	@echo "make clean     remove generated data (raw is regenerable from seed)"

setup:
	$(PY) -m pip install -r requirements.txt

data:
	$(PY) -m src.pipeline generate --config $(CONFIG)

ingest:
	$(PY) -m src.pipeline ingest --config $(CONFIG)

validate:
	$(PY) -m src.pipeline validate --config $(CONFIG)

transform:
	$(PY) -m src.pipeline transform --config $(CONFIG)

cohort:
	$(PY) -m src.pipeline cohort --config $(CONFIG)

analyze:
	$(PY) -m src.pipeline analyze --config $(CONFIG)

pipeline:
	$(PY) -m src.pipeline all --config $(CONFIG)

analyze-r:
	Rscript r/analysis.R

test:
	$(PY) -m pytest tests/ -q

lint:
	$(PY) -m ruff check src tests || true

clean:
	rm -rf data
