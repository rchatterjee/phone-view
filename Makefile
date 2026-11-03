# Release helpers for PhoneView.
#
#   make release VERSION=1.0.4   bump, test, build, commit, tag, push, and
#                                create the GitHub release (which triggers
#                                .github/workflows/publish.yml -> PyPI)
#
# Other targets: make version | test | build | bump VERSION=x.y.z | clean

PYTHON  ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
# The SSH remote isn't set up on this machine, so push over HTTPS (gh auth).
REMOTE  ?= https://github.com/rchatterjee/phone-view.git
BRANCH  ?= main
INIT    := src/phoneview/__init__.py
CURRENT := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' $(INIT))
TAG      = v$(VERSION)

.PHONY: help version test build clean bump release check-version check-clean

help:
	@sed -n '1,8s/^# \{0,1\}//p' Makefile

version:
	@echo $(CURRENT)

test:
	$(PYTHON) -m unittest discover -s tests
	node tests/test_viewer_js.cjs

build: clean
	$(PYTHON) -m build
	$(PYTHON) -m twine check --strict dist/*

clean:
	rm -rf dist build src/*.egg-info

check-version:
	@test -n "$(VERSION)" || { echo "Usage: make $(MAKECMDGOALS) VERSION=x.y.z (current: $(CURRENT))"; exit 1; }
	@echo "$(VERSION)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo "VERSION must look like 1.2.3"; exit 1; }
	@! git rev-parse -q --verify "refs/tags/$(TAG)" >/dev/null || { echo "Tag $(TAG) already exists locally"; exit 1; }
	@! git ls-remote --exit-code --tags $(REMOTE) "refs/tags/$(TAG)" >/dev/null || { echo "Tag $(TAG) already exists on GitHub"; exit 1; }

check-clean:
	@test "$$(git rev-parse --abbrev-ref HEAD)" = "$(BRANCH)" || { echo "Not on $(BRANCH)"; exit 1; }
	@git diff --quiet && git diff --cached --quiet || { echo "Working tree has uncommitted changes"; exit 1; }

bump: check-version
	sed -i.bak 's/^__version__ = ".*"/__version__ = "$(VERSION)"/' $(INIT) && rm -f $(INIT).bak
	@echo "Version: $(CURRENT) -> $(VERSION)"

release: check-clean check-version
	git pull --ff-only $(REMOTE) $(BRANCH)
	$(MAKE) bump VERSION=$(VERSION)
	$(MAKE) test build
	git commit -am "Release $(TAG)"
	git tag -a $(TAG) -m "$(TAG)"
	git push $(REMOTE) $(BRANCH) $(TAG)
	gh release create $(TAG) --title "$(TAG)" --generate-notes
	@echo "Released $(TAG). Watch the publish run with: gh run watch"
