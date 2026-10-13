#!/usr/bin/env bash
# verify-kustomize-render.sh [overlay_dir=k8s/overlays/buildmy-prod] [baseline_ref=origin/main]
# Renders the overlay from the working tree and the baseline tree from <baseline_ref>
# (its overlay if present, else its k8s/ root), then diffs the two outputs.
# Exit 0 = identical, 1 = diff, 2 = FORBID_LITERALS hit.
# SKIP_BASELINE=1: render the overlay only (for new-project overlays that are
# not meant to equal production) and still apply FORBID_LITERALS; prints RENDER OK.
set -euo pipefail

overlay_dir="${1:-k8s/overlays/buildmy-prod}"
baseline_ref="${2:-origin/main}"

if [[ -n "${KUSTOMIZE:-}" ]]; then
  RENDER_CMD=("$KUSTOMIZE" build)
elif command -v kustomize >/dev/null 2>&1; then
  RENDER_CMD=(kustomize build)
elif command -v kubectl >/dev/null 2>&1; then
  RENDER_CMD=(kubectl kustomize)
else
  echo "error: no kustomize renderer found (set \$KUSTOMIZE, or install 'kustomize' or 'kubectl' on PATH)" >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

if [[ -z "${SKIP_BASELINE:-}" ]]; then
  git archive "$baseline_ref" k8s | tar -x -C "$tmpdir"
fi

render() { "${RENDER_CMD[@]}" "$1"; }

baseline_out="$tmpdir/baseline.yaml"
overlay_out="$tmpdir/overlay.yaml"
if [[ -z "${SKIP_BASELINE:-}" ]]; then
  # Pre-COMP-2 baselines render from the flat k8s/ root (k8s/kustomization.yaml);
  # from COMP-2 on there is no root kustomization, so render the baseline's own
  # overlay entry point instead. Both compare against the same working-tree render.
  if [[ -f "$tmpdir/k8s/overlays/buildmy-prod/kustomization.yaml" ]]; then
    render "$tmpdir/k8s/overlays/buildmy-prod" >"$baseline_out"
  else
    render "$tmpdir/k8s" >"$baseline_out"
  fi
fi
render "$overlay_dir" >"$overlay_out"

if [[ -n "${FORBID_LITERALS:-}" ]]; then
  failed=0
  for lit in $FORBID_LITERALS; do
    if grep -F -- "$lit" "$overlay_out" >/dev/null; then
      echo "FORBIDDEN literal found in overlay render: $lit" >&2
      grep -n -F -- "$lit" "$overlay_out" >&2 || true
      failed=1
    fi
  done
  if [[ "$failed" -ne 0 ]]; then
    exit 2
  fi
fi

if [[ -n "${SKIP_BASELINE:-}" ]]; then
  docs="$(awk '/^---$/{c++} END{print c+1}' "$overlay_out")"
  echo "RENDER OK ($docs documents, baseline skipped)"
  exit 0
fi

if diff -u "$baseline_out" "$overlay_out" >"$tmpdir/diff.txt"; then
  docs="$(awk '/^---$/{c++} END{print c+1}' "$overlay_out")"
  echo "RENDER IDENTICAL ($docs documents)"
  exit 0
else
  cat "$tmpdir/diff.txt"
  exit 1
fi
