#!/usr/bin/env bash
# Creates a fixed-size GKE cluster (no autoscaling, so node pressure stays real)
# and the Artifact Registry repo for the patched checkout image.
set -euo pipefail
source "$(dirname "$0")/../scripts/common.sh"

gcloud config set project "$PROJECT_ID"

log "Creating cluster $CLUSTER_NAME in $ZONE"
gcloud container clusters create "$CLUSTER_NAME" \
  --zone "$ZONE" \
  --num-nodes "$NUM_NODES" \
  --machine-type "$MACHINE_TYPE"

gcloud container clusters get-credentials "$CLUSTER_NAME" --zone "$ZONE"

if ! gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" >/dev/null 2>&1; then
  log "Creating Artifact Registry repo $AR_REPO"
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker --location="$REGION"
fi
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

log "Cluster ready"
