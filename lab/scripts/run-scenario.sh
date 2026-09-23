#!/usr/bin/env bash
# Runs the checkout retry storm end to end:
#   healthy release -> baseline -> fault release -> incident -> rollback
# and captures cluster state at each stage under lab/runs/<run id>/.
#
# KEEP_FAULT=1 leaves the incident running at the end, for agent chapters.
set -euo pipefail
source "$(dirname "$0")/common.sh"

KEEP_FAULT="${KEEP_FAULT:-0}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$LAB_DIR/runs/$RUN_ID"
CAPTURE="$LAB_DIR/scripts/capture.sh"
mkdir -p "$RUN_DIR"

if [[ "${USE_PATCHED_CHECKOUT:-0}" != "1" ]]; then
  echo "The fault needs the patched checkout. Run make build, then set USE_PATCHED_CHECKOUT=1." >&2
  exit 1
fi
if grep -q "CHANGE_ME" "$HELM_DIR/values-fault.yaml"; then
  echo "Set PAYMENT_TIMEOUT in lab/helm/values-fault.yaml first." >&2
  exit 1
fi

HEALTHY_REV=""
FAULT_APPLIED=0

rollback() {
  if [[ "$FAULT_APPLIED" == "1" && -n "$HEALTHY_REV" ]]; then
    log "Rolling back to healthy revision $HEALTHY_REV"
    helm rollback "$RELEASE" "$HEALTHY_REV" -n "$NAMESPACE" --wait
    FAULT_APPLIED=0
  fi
}
on_interrupt() { log "Interrupted"; rollback; exit 130; }
trap on_interrupt INT TERM

log "Run $RUN_ID: applying healthy release"
helm_release
HEALTHY_REV="$(current_revision)"
echo "healthy_revision=$HEALTHY_REV" >> "$RUN_DIR/run.env"
"$CAPTURE" "$RUN_DIR" 1-healthy

log "Baseline for ${BASELINE_SECONDS}s"
sleep "$BASELINE_SECONDS"
"$CAPTURE" "$RUN_DIR" 2-baseline

log "Applying fault release"
echo "fault_applied_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$RUN_DIR/run.env"
FAULT_APPLIED=1
helm_release -f "$HELM_DIR/values-fault.yaml"
echo "fault_revision=$(current_revision)" >> "$RUN_DIR/run.env"

log "Incident running for ${FAULT_SECONDS}s"
sleep "$FAULT_SECONDS"
"$CAPTURE" "$RUN_DIR" 3-incident

if [[ "$KEEP_FAULT" == "1" ]]; then
  FAULT_APPLIED=0
  log "KEEP_FAULT=1: leaving the incident running. Recover with: make rollback"
else
  rollback
  echo "recovered_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$RUN_DIR/run.env"
  "$CAPTURE" "$RUN_DIR" 4-recovered
fi

log "Done. Stage timestamps and revisions: $RUN_DIR/run.env"
log "Use them to set time windows in Jaeger and Grafana for screenshots."
