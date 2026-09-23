#!/usr/bin/env bash
# Shared settings for lab scripts. Source this; don't run it.

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "$LAB_DIR/config.env" ]]; then
  echo "Missing lab/config.env. Copy lab/config.env.example and fill it in." >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$LAB_DIR/config.env"

: "${CHART_VERSION:?Set CHART_VERSION in lab/config.env}"

CHECKOUT_IMAGE_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/checkout"
HELM_DIR="$LAB_DIR/helm"

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

# helm_release <extra args...>: upgrade the demo on the pinned chart version,
# with the healthy values as the base and, once USE_PATCHED_CHECKOUT=1, the
# patched checkout image.
helm_release() {
  local image_args=()
  if [[ "${USE_PATCHED_CHECKOUT:-0}" == "1" ]]; then
    image_args=(
      --set "components.checkout.imageOverride.repository=${CHECKOUT_IMAGE_REPO}"
      --set "components.checkout.imageOverride.tag=${CHECKOUT_TAG}"
    )
  fi
  helm upgrade --install "$RELEASE" open-telemetry/opentelemetry-demo \
    --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" --create-namespace \
    -f "$HELM_DIR/values-lab.yaml" \
    ${image_args[@]+"${image_args[@]}"} \
    --wait --timeout 10m \
    "$@"
}

current_revision() {
  helm history "$RELEASE" -n "$NAMESPACE" --max 1 -o json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["revision"])'
}
