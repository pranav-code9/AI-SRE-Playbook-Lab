#!/usr/bin/env bash
# Copies the lab patch into the demo source, builds the checkout image and
# pushes it to Artifact Registry.
set -euo pipefail
source "$(dirname "$0")/common.sh"

SRC="$(cd "$LAB_DIR" && cd "$DEMO_SRC" && pwd)"
cp "$LAB_DIR/patches/checkout/payment_retry.go" "$SRC/src/checkout/"

if ! grep -q "chargeWithRetry" "$SRC/src/checkout/main.go"; then
  echo "main.go doesn't call chargeWithRetry yet. See lab/patches/checkout/README.md step 2." >&2
  exit 1
fi

IMAGE="${CHECKOUT_IMAGE_REPO}:${CHECKOUT_TAG}"
log "Building $IMAGE"
# VERIFY the Dockerfile path and build context for the pinned demo version.
docker build -t "$IMAGE" -f "$SRC/src/checkout/Dockerfile" "$SRC"
docker push "$IMAGE"
log "Pushed $IMAGE"
