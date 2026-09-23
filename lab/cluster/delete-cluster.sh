#!/usr/bin/env bash
# Deletes the lab cluster. The Artifact Registry repo is kept so images survive.
set -euo pipefail
source "$(dirname "$0")/../scripts/common.sh"

gcloud container clusters delete "$CLUSTER_NAME" --zone "$ZONE" --quiet
