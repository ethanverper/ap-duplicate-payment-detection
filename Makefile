PROJECT ?= ap-duplicate-payment-detection  # same as config/settings.yaml -> project.name
TF := terraform -chdir=infra/terraform
PYTHON ?= python3

.PHONY: help install test lint fmt generate kpis-local grade deploy upload-reference upload-history kpis destroy clean-glue-logs leftovers cost cost-daily

help: ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

install: ## Install dev tools
	$(PYTHON) -m pip install -r requirements-dev.txt

test: ## Unit, moto and local Spark tests (no AWS)
	$(PYTHON) -m pytest -q

lint: ## Python lint + Terraform format check
	ruff check .
	$(TF) fmt -check -recursive

fmt: ## Format Python and Terraform
	ruff format .
	$(TF) fmt -recursive

generate: ## Simulate 90 days of ERP payment files + master data + answer key into data/generated (local, no AWS)
	$(PYTHON) -m src.generators.history --out data/generated

kpis-local: ## Run the Glue job (duplicates + KPIs) on data/generated with a local Spark, into data/lake-local (no AWS)
	$(PYTHON) -m scripts.kpis_local --input data/generated --output data/lake-local

grade: ## Grade the duplicate detector of the local run against the simulator's answer key
	$(PYTHON) -m scripts.grade_detection --lake data/lake-local --truth data/generated/truth/planted_problems.csv

deploy: ## Create the stack (read the cost estimate in docs/spec.md first); saves the start time for clean-glue-logs
	$(TF) init -input=false
	mkdir -p infra/terraform/build && date -u +%s000 > infra/terraform/build/deploy_started_at
	$(TF) apply

upload-reference: ## Upload the master data (suppliers, entities) the Glue job joins on
	aws s3 sync data/generated/reference s3://$$($(TF) output -raw bucket)/reference/

upload-history: ## Upload the 90 days of ERP files to landing/: every file triggers the validator Lambda
	aws s3 sync data/generated/landing s3://$$($(TF) output -raw bucket)/landing/

kpis: ## Start the Glue job on AWS (~2 DPU x 3 min, about $0.05 a run); prints the run id
	aws glue start-job-run --job-name $$($(TF) output -raw glue_job_name) --query JobRunId --output text

destroy: ## Delete everything
	$(TF) destroy

clean-glue-logs: ## After destroy: delete the log groups Glue created during this deploy (Terraform can't)
	scripts/clean_glue_logs.sh

leftovers: ## Check nothing billable is still running (three independent checks)
	scripts/leftovers.sh $(PROJECT)

cost: ## Actual cost of this project (Cost Explorer, tag Project; needs the tag activated)
	scripts/cost.sh $(PROJECT)

cost-daily: ## Account cost by service and day, last 7 days (no tag needed; check the day after a deploy)
	scripts/cost.sh --daily
