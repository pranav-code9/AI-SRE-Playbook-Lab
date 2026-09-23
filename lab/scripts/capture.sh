#!/usr/bin/env bash
# capture.sh <output dir> <stage label>
# Snapshots cluster state for one stage of a scenario run.
set -euo pipefail
source "$(dirname "$0")/common.sh"

OUT="$1/$2"
mkdir -p "$OUT"

date -u +%Y-%m-%dT%H:%M:%SZ > "$OUT/timestamp"
helm history "$RELEASE" -n "$NAMESPACE" > "$OUT/helm-history.txt"
kubectl -n "$NAMESPACE" get pods -o wide > "$OUT/pods.txt"
kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp > "$OUT/events.txt"
kubectl -n "$NAMESPACE" get hpa > "$OUT/hpa.txt" 2>&1 || true
kubectl -n "$NAMESPACE" top pods > "$OUT/top-pods.txt" 2>&1 || true
kubectl top nodes > "$OUT/top-nodes.txt" 2>&1 || true
kubectl describe nodes | grep -A6 "Conditions:" > "$OUT/node-conditions.txt" || true

log "Captured stage '$2' in $OUT"
