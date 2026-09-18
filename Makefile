.PHONY: test test-unit pytest compile check compatibility-audit compatibility-build

PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest
PROFILE ?= template_profiles/cau-graduate-thesis-2025-winter/profile.json
TRIMMED_MASTER ?= build/cau-template-engine/cau-official-trimmed-master.docx
COMPAT_DOCX ?= build/cau-template-engine/cau-wps-word-compatible-editing.docx
COMPAT_MANIFEST ?= build/cau-template-engine/word-finalization-manifest.json
COMPAT_AUDIT ?= build/cau-template-engine/static-ooxml-compatibility.json

compile:
	$(PYTHON) -m compileall -q scripts tests

test-unit:
	$(PYTHON) -m unittest discover -s tests -v

pytest:
	$(PYTEST)

test: compile test-unit

compatibility-audit:
	$(PYTHON) scripts/ooxml_compatibility.py $(TRIMMED_MASTER) --out $(COMPAT_AUDIT)

compatibility-build:
	$(PYTHON) scripts/compatibility_builder.py $(PROFILE) $(COMPAT_DOCX) \
		--source $(TRIMMED_MASTER) --manifest $(COMPAT_MANIFEST)

check: test
	git diff --check
