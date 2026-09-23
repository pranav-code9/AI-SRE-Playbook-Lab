SCRIPTS := lab/scripts
CLUSTER := lab/cluster

.PHONY: help cluster destroy repo-add build install fault rollback hpa run run-keep capture forward

help:
	@echo "cluster    Create the GKE cluster and Artifact Registry repo"
	@echo "build      Build and push the patched checkout image"
	@echo "install    Install the demo with the patched checkout (healthy state)"
	@echo "hpa        Apply autoscalers for checkout and payment"
	@echo "forward    Port-forward the frontend proxy to localhost:8080"
	@echo "fault      Apply the fault release (starts the incident)"
	@echo "rollback   Roll back one release (ends the incident)"
	@echo "run        Full scenario: healthy, baseline, fault, rollback"
	@echo "run-keep   Full scenario, leaving the incident running"
	@echo "capture    Snapshot current cluster state"
	@echo "destroy    Delete the cluster"

cluster:
	$(CLUSTER)/create-cluster.sh

destroy:
	$(CLUSTER)/delete-cluster.sh

repo-add:
	helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
	helm repo update

build:
	$(SCRIPTS)/build-checkout.sh

install: repo-add
	bash -c 'source $(SCRIPTS)/common.sh && helm_release'

fault:
	bash -c 'source $(SCRIPTS)/common.sh && helm_release -f $$HELM_DIR/values-fault.yaml'

rollback:
	bash -c 'source $(SCRIPTS)/common.sh && helm rollback $$RELEASE -n $$NAMESPACE --wait'

hpa:
	bash -c 'source $(SCRIPTS)/common.sh && kubectl -n $$NAMESPACE apply -f lab/k8s/hpa.yaml'

forward:
	bash -c 'source $(SCRIPTS)/common.sh && kubectl -n $$NAMESPACE port-forward svc/frontend-proxy 8080:8080'

run:
	$(SCRIPTS)/run-scenario.sh

run-keep:
	KEEP_FAULT=1 $(SCRIPTS)/run-scenario.sh

capture:
	$(SCRIPTS)/capture.sh lab/runs/manual $$(date -u +%Y%m%dT%H%M%SZ)
