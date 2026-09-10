# Installer for the `bootstrap` project generator itself.
#
# This Makefile is completely independent of the Makefiles bootstrap.py
# generates for user projects (those provide all/archive/run/test/clean/
# fclean/re). It is never copied into a generated project. Its only job is
# installing/uninstalling the `bootstrap` command and its default config.

SHELL := /bin/sh

INSTALL_DIR  := $(HOME)/.bootstrap
BOOTSTRAP_BIN := $(INSTALL_DIR)/bootstrap
CONFIG_DST   := $(INSTALL_DIR)/config.json
SOURCE       := bootstrap.py
PATH_MARKER  := \# bootstrap project generator

.PHONY: install uninstall

install:
	@mkdir -p "$(INSTALL_DIR)"
	@cp "$(SOURCE)" "$(BOOTSTRAP_BIN)"
	@chmod +x "$(BOOTSTRAP_BIN)"
	@echo "Installed $(BOOTSTRAP_BIN)"
	@if [ -f "$(CONFIG_DST)" ]; then \
		echo "Existing configuration found at $(CONFIG_DST) -- leaving it untouched."; \
	else \
		printf '%s\n' \
			'{' \
			'    "src_dir": "src",' \
			'    "source_ex": ".c",' \
			'    "header_ex": ".h",' \
			'    "inc_dir": "inc",' \
			'    "doc_dir": "doc",' \
			'    "test_dir": "test",' \
			'    "test_main": "main.sh",' \
			'    "git_init": true,' \
			'    "on_dep_failure": "warn",' \
			'    "dep": {' \
			'        "libft": {' \
			'            "source": "/home/ria/Downloads/libft",' \
			'            "link": "-Llib/libft -lft"' \
			'        },' \
			'        "mlx": {' \
			'            "source": "https://github.com/42school/mlx_CLXV.git",' \
			'            "link": "-lm -lmlx -lXext -lX11"' \
			'        }' \
			'    }' \
			'}' \
			> "$(CONFIG_DST)"; \
		echo "Created default configuration at $(CONFIG_DST)"; \
	fi
	@shell_name=$$(basename "$${SHELL:-sh}" 2>/dev/null); \
	case "$$shell_name" in \
		zsh) rc="$$HOME/.zshrc" ;; \
		bash) rc="$$HOME/.bashrc" ;; \
		*) rc="$$HOME/.profile" ;; \
	esac; \
	marker='$(PATH_MARKER)'; \
	line='export PATH="$$HOME/.bootstrap:$$PATH"'; \
	touch "$$rc"; \
	if grep -qF "$$marker" "$$rc" 2>/dev/null; then \
		echo "PATH entry already present in $$rc -- leaving it as is."; \
	else \
		printf '\n%s\n%s\n' "$$marker" "$$line" >> "$$rc"; \
		echo "Added PATH entry to $$rc"; \
	fi; \
	( . "$$rc" ) >/dev/null 2>&1 || true; \
	echo "Bootstrap installed successfully."; \
	echo "Reload your shell with: source $$rc"

uninstall:
	@rm -f "$(BOOTSTRAP_BIN)" "$(CONFIG_DST)"
	@if [ -d "$(INSTALL_DIR)" ]; then \
		rmdir "$(INSTALL_DIR)" 2>/dev/null || rm -rf "$(INSTALL_DIR)"; \
	fi
	@echo "Removed $(INSTALL_DIR)"
	@shell_name=$$(basename "$${SHELL:-sh}" 2>/dev/null); \
	case "$$shell_name" in \
		zsh) rc="$$HOME/.zshrc" ;; \
		bash) rc="$$HOME/.bashrc" ;; \
		*) rc="$$HOME/.profile" ;; \
	esac; \
	if [ -f "$$rc" ]; then \
		tmp="$$rc.bootstrap.tmp"; \
		awk 'BEGIN{skip=0} { if (skip>0) {skip--; next} if ($$0=="$(PATH_MARKER)") {skip=1; next} print }' "$$rc" > "$$tmp" && mv "$$tmp" "$$rc"; \
		echo "Removed PATH entry from $$rc (if it was present)."; \
	fi; \
	( . "$$rc" ) >/dev/null 2>&1 || true; \
	echo "Bootstrap uninstalled."; \
	echo "Reload your shell with: source $$rc"
