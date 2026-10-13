#!/usr/bin/env bash
# verify-kustomize-render.sh [overlay_dir=k8s/overlays/buildmy-prod] [baseline_ref=origin/main]
# Renders the overlay from the working tree and the k8s/ tree from <baseline_ref>,
# then diffs the two outputs. Exit 0 = identical, 1 = diff, 2 = FORBID_LITERALS hit.
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

git archive "$baseline_ref" k8s | tar -x -C "$tmpdir"

render() { "${RENDER_CMD[@]}" "$1"; }

baseline_out="$tmpdir/baseline.yaml"
overlay_out="$tmpdir/overlay.yaml"
render "$tmpdir/k8s" >"$baseline_out"
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

if diff -u "$baseline_out" "$overlay_out" >"$tmpdir/diff.txt"; then
  docs="$(awk '/^---$/{c++} END{print c+1}' "$overlay_out")"
  echo "RENDER IDENTICAL ($docs documents)"
  exit 0
else
  cat "$tmpdir/diff.txt"
  exit 1
fi
