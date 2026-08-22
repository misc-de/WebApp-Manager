# CLAUDE.md

Guidance for Claude Code (and human contributors) when working in this repository.

## What this is

GTK4 / Libadwaita desktop and mobile app, written in Python 3, that creates and
manages Linux web-app launchers with dedicated browser profiles. Targets Firefox,
Chrome and Chromium. App-ID `de.cais.webappmanager`.

## How to run

```
python3 main.py
```

`main.py` only execs `webapp-manager.py` — that is the real entry point
(`WebAppManager(Adw.Application)` → `MainWindow`). System dependencies:
GTK 4, Libadwaita 1, GObject Introspection.

Python dependencies are declared in [requirements.txt](requirements.txt)
(`PyGObject`, `Pillow`), [requirements-optional.txt](requirements-optional.txt)
(`cairosvg` — gates SVG icon import; `GtkSource` typelib — gates the code editor
in the custom-asset dialog) and [requirements-dev.txt](requirements-dev.txt)
(ruff, mypy, coverage). On most distributions the packaged `python3-gi` build is
preferable to a pip install, because it matches the system GTK exactly.

User data lives in:
- `~/.config/webapp-manager/config.json` — language, settings, window state
- `~/.local/share/webapp-manager/webappmanager.db` — entries + options (SQLite, WAL)
- `~/.local/state/webapp/app.log` — rotated app log
- `~/.mozilla/firefox/webapp_*` and `~/.config/webapp-browser-profiles/{chrome,chromium}/` — managed browser profiles

## Architecture map

### Data layer
- [database.py](database.py) — SQLite wrapper. Versioned via `PRAGMA user_version`,
  migrations live in the module-level `MIGRATIONS` dict. Two tables:
  `entries(id, title, description, active)` and
  `options(id, entry_id, option_key, option_value)` with cascade delete and
  unique index on `(entry_id, option_key)`.
- [app_models.py](app_models.py) — `Entry` GObject for GTK list models.
- [app_state.py](app_state.py) — `WebAppState` dataclass.
- [webapp_constants.py](webapp_constants.py) — every option key, alias map,
  filesystem roots (`FIREFOX_ROOT`, `CHROMIUM_PROFILE_ROOT`, `APPLICATIONS_DIR`).

### Browser-options system (the heart of the app)
- [browser_option_registry.py](browser_option_registry.py) — declarative
  `BrowserOptionSpec`s with per-family `BrowserOptionBinding`s. Categories:
  `security`, `cleanup`, `performance`, `comfort`, `addons`. Each option states
  which families support it and how (`profile_setting`, `extension_action`,
  `app_logic`, `shutdown_cleanup`, `macro`).
- [browser_option_logic.py](browser_option_logic.py) — option normalisation,
  semantic mode (`standard` / `kiosk` / `app` / `seamless`), browser-state
  encoding for round-tripping.
- [option_config.py](option_config.py), [detail_page/option_state.py](detail_page/option_state.py) — UI binding helpers.

**Adding a new option:** define a new `BrowserOptionSpec` in
`_VISIBLE_BROWSER_OPTION_SPECS`, add the key constant in `webapp_constants.py`,
add a UI label key in `lang/en.json`, then implement the actual behaviour in
the relevant binding kind (e.g. `_write_firefox_user_js` for `profile_setting`).

### Browser profile + extension management
Split across four modules forming a one-directional dependency graph
`browser_paths → {browser_settings, browser_extensions} → browser_profiles`:
- [browser_paths.py](browser_paths.py) — leaf foundation: profile-root math,
  managed-profile markers, family detection, `_safe_remove_tree`,
  `get_firefox_extension_config`. Imports only stdlib + `webapp_constants` +
  `i18n`, never its siblings.
- [browser_settings.py](browser_settings.py) — `ProfileSettings` value object,
  Firefox `user.js` / Chromium `Preferences` writers, runtime-cache clearing,
  app-mode `userChrome.css`, and the round-trip readers (`read_profile_settings`).
- [browser_extensions.py](browser_extensions.py) — extension discovery, payload
  loading/scoping, signature heuristics, zip-slip guards
  (`_assert_safe_zip_members`) and the install/sync state machine (uBlock Origin,
  swipe-gestures).
- [browser_profiles.py](browser_profiles.py) — profile lifecycle (creation,
  copy, Firefox `profiles.ini` registration, deletion guarded by
  `_safe_remove_tree`), the `apply_profile_settings` orchestrator and browser
  command resolution. Re-exports the leaf modules' public symbols so historical
  `from browser_profiles import X` call sites keep working. Touch with care.
  Note for tests: functions that read module-level globals
  (`FIREFOX_ROOT`, `get_firefox_extension_config`, `is_furios_distribution`,
  `urllib`, `__file__`) now live in the leaf modules, so `mock.patch` must
  target the module that *reads* the name (e.g. `browser_paths.FIREFOX_ROOT`,
  `browser_extensions.get_firefox_extension_config`).
- [engine_support.py](engine_support.py) — engine availability detection
  (cached on first call). Exposes a single module-level `ENGINES =
  available_engines()` snapshot that the UI modules import directly
  (`from engine_support import ENGINES`); it is never mutated, so all consumers
  share one list.

### Desktop integration
- [desktop_entries.py](desktop_entries.py) — builds `Exec=` line via `shlex.join`
  (never `shell=True`), writes `.desktop` files to `~/.local/share/applications`
  with `ManagedBy=<i18n value>`, `EntryId=<id>`, `X-WebApp-*` metadata. When
  the per-form-factor modes (`Mode (Mobile)` vs `Mode (Desktop)`) diverge,
  `Exec=` points at a wrapper script generated by `launcher_wrapper.py`;
  otherwise it points at the browser directly.
- [launcher_wrapper.py](launcher_wrapper.py) — per-WebApp shell wrapper living
  at `~/.local/share/webapp-manager/launchers/<slug>.sh`. Detects form factor
  at launch via `$XDG_CURRENT_DESKTOP` / `$XDG_SESSION_DESKTOP`
  (phosh / plasma-mobile) and `/etc/furios-release`; `$WEBAPP_FORM=mobile|desktop`
  as manual override. Only written when the two configured modes actually
  differ; deleted automatically when they converge again.
- [icon_pipeline.py](icon_pipeline.py) — PNG normalisation via Pillow,
  SVG via cairosvg with an explicit `_block_external_svg_resource` URL fetcher
  (SSRF mitigation).
- [manager_integration.py](manager_integration.py) — installs the Manager's own
  `.desktop` launcher.

### UI
- [webapp-manager.py](webapp-manager.py) — `MainWindow(Adw.ApplicationWindow)`,
  composed from 8 mixins re-exported by [mainwindow/](mainwindow/__init__.py):
  `MainWindowWindowStateMixin`, `MainWindowLaunchExportMixin`,
  `MainWindowNotificationsMixin`, `MainWindowSettingsMixin`,
  `MainWindowDialogsMixin`, `MainWindowProfileImportMixin`,
  `MainWindowOverviewMixin`, `MainWindowEntriesMixin`. Each mixin lives in
  its own module: [mainwindow/dialogs.py](mainwindow/dialogs.py),
  [mainwindow/entries.py](mainwindow/entries.py), etc.
- [detail_page/](detail_page/__init__.py) — per-entry editor package.
  `DetailPage` is composed from 5 mixins; entry point is
  [detail_page/page.py](detail_page/page.py). Submodules: `assets`, `icon`,
  `layout`, `options`, `option_state`, `transfer`.
- [style.css](style.css) — GTK CSS, uses Libadwaita named colours
  (`@accent_bg_color`, `@window_bg_color`) plus the GTK-specific `alpha(@…)`
  function. The VS Code CSS linter flags the latter as invalid — it is valid
  GTK CSS, just not standard CSS. Ignore those diagnostics.

### Cross-cutting
- [i18n.py](i18n.py) — translation lookup with `t(key, **kwargs)`. Translations
  in [lang/](lang/) (37 languages). User language overrides config; system
  language is auto-detected.
- [input_validation.py](input_validation.py) — URL validation, slug building,
  `.wapp` payload normalisation, origin reachability check. Single source of
  truth for what counts as a "safe" URL/value before it hits the FS or
  subprocess.
- [logger_setup.py](logger_setup.py) — `RotatingFileHandler` (1 MB × 3 backups
  by default) + stderr stream. Level via `WEBAPP_MANAGER_LOG_LEVEL` env var
  (`DEBUG` / `INFO` / `WARNING` / `ERROR`).
- [custom_assets.py](custom_assets.py) — per-WebApp custom CSS/JS injection.
  Library lives in `settings.custom_assets`. Builds a tiny Firefox extension
  XPI / Chromium extension dir per WebApp profile.

### Tests
- [tests/](tests/) — pure `unittest`, no pytest. Each test file stubs
  `logger_setup` via a dummy module so the real logger does not write to disk.
  [tests/test_outbound_request_guard.py](tests/test_outbound_request_guard.py)
  is the exception that talks to the network: it starts a loopback HTTP server,
  because the redirect guard hooks into urllib's redirect machinery and cannot
  be exercised by stubbing a single function. It skips itself where loopback
  sockets are unavailable.
  [tests/test_plugin_feedback.py](tests/test_plugin_feedback.py) shows the
  pattern for testing UI logic without a widget tree: a stub object borrows the
  real methods and hand-implements what they call back into.
- Run: `python3 -m unittest discover -s tests -v`
- CI: [.github/workflows/test.yml](.github/workflows/test.yml) has two jobs.
  `test` installs the distribution's GTK 4 / Libadwaita bindings via apt and
  runs the full suite with coverage — four test modules import `mainwindow` /
  `detail_page` and therefore need PyGObject. `quality` runs byte-compilation,
  ruff and mypy across Python 3.11 / 3.12 / 3.13 without GTK, which is what
  gives the version matrix.

## Conventions

- **Subprocess launches** must use `subprocess.Popen(argv, …)` with a list and
  never `shell=True`. The argv is built token-by-token in
  `desktop_entries.build_launch_command`. URLs that hit `Exec=` are first run
  through `is_valid_url` (whitelisted to `http`/`https`) and `normalize_address`.
- **Filesystem deletion** of profiles must go through
  `browser_profiles._safe_remove_tree`, which refuses to descend outside of
  `FIREFOX_ROOT` / `CHROMIUM_PROFILE_ROOT`.
- **Zip extraction** of any external/downloaded archive must be guarded by
  `browser_profiles._assert_safe_zip_members` before `extractall` — this rejects
  `..`, absolute paths and `\`.
- **Outbound HTTP requests** (favicon fetch, icon discovery, extension
  download) must go through `input_validation.open_guarded_url`, never through
  `urllib.request.urlopen` directly. It validates the scheme on the initial URL
  *and on every redirect hop*, and blocks a public → private/loopback pivot.
  Reaching a private host directly stays allowed on purpose — running a web app
  against `http://192.168.1.10:8080` is a normal use of this program — so pass
  `allow_private_targets=False` only where no LAN target is ever legitimate
  (currently the extension download). Covered by
  `tests/test_outbound_request_guard.py`.
- **Launch-mode decisions** are split across two keys: `Mode (Mobile)` and
  `Mode (Desktop)`. When either is absent (legacy entries), it falls back to
  `semantic_mode_from_options(options)` — the legacy `Kiosk` / `App Mode` /
  `Frameless` triple. `mobile_mode_value()` / `desktop_mode_value()` in
  `browser_option_logic.py` are the single entry points for resolving the
  actual mode.
- **Database changes** require a new entry in `MIGRATIONS` plus an incremented
  `SCHEMA_VERSION`. The `Database.__init__` will auto-backup the file before
  applying.
- **I18n keys**: every new key goes into `lang/en.json` first (the canonical
  reference). `tests/test_i18n_integrity.py` enforces that no other language
  defines unknown keys and that placeholders match.

## Things that look weird but are intentional

- The `_options_cache` on `MainWindow` stores per-entry option dicts with
  canonical keys; populated in `load_entries_from_db` via
  `normalize_option_rows`. Direct `db.list_option_values()` rows must be passed
  through `normalize_option_rows` before caching, otherwise legacy aliases
  leak into the cache.
- `_xpi_has_signature` only checks for the presence of `META-INF/` files. It is
  a heuristic, not a real signature verification — Firefox itself does the
  cryptographic check. Do not rename without thinking through the consequences.

## Known gaps

- No Flatpak manifest / packaging — installation is `git clone` + `python3 main.py`.
  A full `src/webapp_manager/` package layout pairs naturally with this and
  would also let the `mainwindow`/`detail_page` packages move under the
  package root.
- `MainWindow` mixin hierarchy is wide (8 mixins). Consider splitting into
  composed controllers when next refactoring.
- UI test coverage is thin. The logic layer is reasonably covered
  (`browser_option_logic` ~83%, `database` ~80%, `launcher_wrapper` ~86%), but
  the mixin modules sit in the 7–17% range, so the ~5k lines of UI code —
  including all worker-thread handling — are effectively untested.
- `check_untyped_defs` is enabled for 32 of 45 modules. The 13 UI mixins are
  excluded in [pyproject.toml](pyproject.toml) because each mixin reads
  attributes owned by the composed class (~880 `attr-defined` findings). Remove
  a module from that override list once its mixin declares those attributes —
  see `MainWindowProfileImportMixin._profile_resync_cancel_event` for the
  pattern.
- Partial translation completeness gate — `tests/test_i18n_integrity.py` now
  enforces 100% coverage for the locales in `REQUIRED_COMPLETE_LANGUAGES`
  (currently `en` reference + `de`); the other ~34 locales sit at ~74% and are
  only reported, not gated. Add a language to that set once it is fully
  translated.
