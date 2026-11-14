SHELL := bash
export KUBECONFIG ?= $(CURDIR)/.kube/config
TAG ?= 1.0.0
URL ?= http://localhost:30080

.PHONY: help up cluster build load deploy demo chaos test lint jenkins down clean

help:            ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

up: cluster build load-all deploy  ## One command: cluster + images + full stack

cluster:         ## Start single-node k3s inside Docker
	bash scripts/cluster-up.sh

build:           ## Build both images
	docker build -t self-healing/demo-service:$(TAG) services/demo-service
	docker build -t self-healing/healer:$(TAG) healer

load:            ## Import app images into the cluster
	IMAGE_TAG=$(TAG) bash scripts/load-images.sh

load-all:        ## Import app + monitoring images (avoids pulls inside the cluster)
	IMPORT_INFRA=1 IMAGE_TAG=$(TAG) bash scripts/load-images.sh

deploy:          ## Apply manifests and wait for rollouts
	IMAGE_TAG=$(TAG) bash scripts/deploy.sh

chaos:           ## Run the automated chaos gate against the live system
	python scripts/chaos_test.py --url $(URL)

test:            ## Unit + API tests
	python -m pytest -q --cov=healer/healer

lint:
	ruff check healer services tests

jenkins:         ## Start Jenkins with the pipeline pre-configured
	bash scripts/jenkins-up.sh

down:            ## Delete the k3s cluster container
	docker rm -f shk-k3s

clean: down
	docker compose -f jenkins/docker-compose.yml down -v || true
