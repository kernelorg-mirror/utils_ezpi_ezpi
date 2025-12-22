# Minimal makefile for Sphinx documentation

SPHINXBUILD   = sphinx-build
SOURCEDIR     = .
BUILDDIR      = _build

.PHONY: help clean html

help:
	@echo "Available targets:"
	@echo "  html    - build HTML documentation"
	@echo "  clean   - remove built documentation"

html:
	$(SPHINXBUILD) -b html $(SOURCEDIR) $(BUILDDIR)/html

clean:
	rm -rf $(BUILDDIR)
