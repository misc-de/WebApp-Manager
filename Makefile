# WebApp Manager -- Flatpak build and repository publishing.
#
# The repository this produces is what users install from:
#
#   flatpak remote-add --if-not-exists webappmanager \
#       https://misc-de.github.io/WebApp-Manager/de.cais.webappmanager.flatpakrepo
#   flatpak install webappmanager de.cais.webappmanager
#
# Typical release, both architectures:
#   x86_64 :  make flatpak-build                      (on this machine)
#   aarch64:  on the phone `make flatpak-build`, copy the repo/ it produced
#             back here, then `make flatpak-merge ARM_REPO=repo-arm`
#   finally:  make flatpak-publish && make flatpak-pages

APPID       = de.cais.webappmanager
FP_MANIFEST ?= flatpak/$(APPID).yml
FP_REPO     ?= repo
FP_ARCH      = $(shell flatpak --default-arch)
FP_BUILDDIR ?= .flatpak-build/$(FP_ARCH)
# Signing key. The private half lives in .gnupg-flatpak/, which is gitignored
# and exists only on the maintainer's machine -- back it up separately.
FP_GPG      ?= flatpak@cais.de
FP_GPGHOME  ?= $(CURDIR)/.gnupg-flatpak
FP_GPGARGS   = $(if $(FP_GPG),--gpg-sign=$(FP_GPG) $(if $(FP_GPGHOME),--gpg-homedir=$(FP_GPGHOME)),)
FP_BUILDER   = $(shell command -v flatpak-builder >/dev/null 2>&1 \
		&& echo flatpak-builder || echo flatpak run org.flatpak.Builder)
PAGES_BRANCH ?= gh-pages
PAGES_WORKTREE = .pages-worktree

.PHONY: test lint flatpak-build flatpak-merge flatpak-publish flatpak-repo-info \
	flatpak-repofile flatpak-pages flatpak-key

# --- Development ----------------------------------------------------------

test:
	python3 -m unittest discover -s tests -v

lint:
	ruff check .
	mypy .

# --- Flatpak --------------------------------------------------------------

# Builds the current host architecture into $(FP_REPO), signed.
flatpak-build:
	$(FP_BUILDER) --force-clean --repo=$(FP_REPO) $(FP_GPGARGS) \
		$(FP_BUILDDIR) $(FP_MANIFEST)
	@echo "$(FP_ARCH) is in $(FP_REPO)/. Refs: make flatpak-repo-info"

# Merges a repo built on another architecture (ARM_REPO=<path>) and signs the
# commits it brought in. The phone has no access to the signing key, so it
# builds unsigned; without this second step flatpak would reject the aarch64
# ref on any remote with gpg-verify enabled -- which is every normal install.
flatpak-merge:
	@test -n "$(ARM_REPO)" || { echo "pass ARM_REPO=<path> (the repo/ copied from the phone)"; exit 1; }
	ostree --repo=$(FP_REPO) pull-local $(ARM_REPO)
	@if [ -n "$(FP_GPG)" ]; then \
		for ref in $$(ostree --repo=$(ARM_REPO) refs); do \
			commit=$$(ostree --repo=$(FP_REPO) rev-parse $$ref) || continue; \
			ostree --repo=$(FP_REPO) gpg-sign \
				$(if $(FP_GPGHOME),--gpg-homedir=$(FP_GPGHOME),) \
				$$commit $(FP_GPG) >/dev/null 2>&1 \
				&& echo "signed $$ref" || echo "already signed: $$ref"; \
		done; \
	fi
	@echo "Merged. Next: make flatpak-publish"

# Writes summary/AppStream/static deltas and signs them.
flatpak-publish: flatpak-repofile
	flatpak build-update-repo --generate-static-deltas --prune $(FP_GPGARGS) $(FP_REPO)
	@echo "$(FP_REPO)/ is ready to host. Next: make flatpak-pages"

# Regenerates the .flatpakrepo from the current public key, so the file can
# never drift from the key the repo is actually signed with.
flatpak-repofile:
	@test -d "$(FP_GPGHOME)" || { echo "no keyring at $(FP_GPGHOME) -- run: make flatpak-key"; exit 1; }
	@printf '%s\n' \
		'[Flatpak Repo]' \
		'Title=WebApp Manager' \
		'Url=https://misc-de.github.io/WebApp-Manager/repo/' \
		'Homepage=https://github.com/misc-de/WebApp-Manager' \
		'Comment=Web app launchers with dedicated browser profiles' \
		'Description=WebApp Manager - GTK4/libadwaita tool for creating Linux web app launchers with their own browser profiles (desktop and mobile).' \
		"GPGKey=$$(GNUPGHOME=$(FP_GPGHOME) gpg --export $(FP_GPG) | base64 -w0)" \
		> data/$(APPID).flatpakrepo
	@echo "data/$(APPID).flatpakrepo regenerated"

# Publishes repo/ and the .flatpakrepo to the $(PAGES_BRANCH) branch.
# Uses a worktree so the main checkout is never touched.
flatpak-pages:
	@test -d $(FP_REPO) || { echo "no $(FP_REPO)/ -- run make flatpak-build first"; exit 1; }
	git show-ref --verify --quiet refs/heads/$(PAGES_BRANCH) \
		|| git branch $(PAGES_BRANCH) $$(git commit-tree $$(git hash-object -t tree /dev/null) -m "Initialise $(PAGES_BRANCH)")
	rm -rf $(PAGES_WORKTREE)
	git worktree add --quiet $(PAGES_WORKTREE) $(PAGES_BRANCH)
	rm -rf $(PAGES_WORKTREE)/repo
	cp -r $(FP_REPO) $(PAGES_WORKTREE)/repo
	cp data/$(APPID).flatpakrepo $(PAGES_WORKTREE)/
	touch $(PAGES_WORKTREE)/.nojekyll
	cd $(PAGES_WORKTREE) && git add -A \
		&& (git diff --cached --quiet || git commit -q -m "Publish $(APPID) $$(date -u +%Y-%m-%dT%H:%MZ)")
	git worktree remove --force $(PAGES_WORKTREE)
	@echo "Committed to $(PAGES_BRANCH). Push it:  git push origin $(PAGES_BRANCH)"

# Shows which app refs (architectures) the repo currently holds.
flatpak-repo-info:
	@ostree --repo=$(FP_REPO) refs 2>/dev/null | grep -E "^app/" | sort \
		|| echo "(no $(FP_REPO) built yet)"

# One-off: creates the signing key. Back up .gnupg-flatpak/ afterwards --
# losing it means every user has to re-add the remote.
flatpak-key:
	@test ! -d "$(FP_GPGHOME)" || { echo "keyring already exists at $(FP_GPGHOME)"; exit 1; }
	mkdir -p $(FP_GPGHOME) && chmod 700 $(FP_GPGHOME)
	printf '%s\n' '%%no-protection' 'Key-Type: eddsa' 'Key-Curve: ed25519' \
		'Key-Usage: sign' 'Name-Real: WebApp Manager Flatpak Repo' \
		'Name-Email: $(FP_GPG)' 'Expire-Date: 0' '%%commit' \
		| GNUPGHOME=$(FP_GPGHOME) gpg --batch --generate-key
	@echo "Key created. Back up $(FP_GPGHOME) somewhere safe."
