KUSTOMIZE ?= kustomize
OVERLAY ?= k8s/overlays/buildmy-prod
BASE ?= origin/main
GATE_OVERLAY ?= k8s/overlays/example-standalone
FORBID_LITERALS ?= buildmyhouse company-ops localhost:30500 homely

KDIRS := $(patsubst %/kustomization.yaml,%,$(shell find k8s/base k8s/projects k8s/overlays -name kustomization.yaml | sort))

.PHONY: components render diff-prod gate-example build-all test check

components:
	@ls -1d k8s/base/* k8s/projects/* k8s/overlays/*

render:
	$(KUSTOMIZE) build $(OVERLAY)

diff-prod:
	KUSTOMIZE="$(KUSTOMIZE)" scripts/verify-kustomize-render.sh k8s/overlays/buildmy-prod $(BASE)

gate-example:
	FORBID_LITERALS="$(FORBID_LITERALS)" SKIP_BASELINE=1 KUSTOMIZE="$(KUSTOMIZE)" scripts/verify-kustomize-render.sh $(GATE_OVERLAY)

build-all:
	@ok=0; \
	for d in $(KDIRS); do \
		echo "render $$d"; \
		$(KUSTOMIZE) build $$d >/dev/null || exit 1; \
		ok=$$((ok+1)); \
	done; \
	echo "build-all OK ($$ok kustomizations)"

test:
	python3 -m pytest -q tests --ignore=tests/test_backup.py

check: build-all diff-prod gate-example test
