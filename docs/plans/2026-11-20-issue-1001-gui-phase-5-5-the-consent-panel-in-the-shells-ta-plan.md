# Shells consent panel implementation plan (#1001)

> **For agentic workers:** REQUIRED SUB-SKILL: Use the sibling `executing-plans` skill for inline execution. Complete each task's test cycle before the next task. Planning review does not authorize implementation before the maintainer approves.

**Goal:** Show and answer held egress requests above workspace terminal tabs.

**Architecture:** Keep `ConsentPresenter` as the state and verdict module. Add a Qt panel and a Shells container. Preserve terminal ownership in `ShellHub` and registration ownership in `WorkspaceDetail`.

**Tech stack:** Python 3.14, PySide6 QtWidgets, existing bridge, pytest with the offscreen Qt platform.

**Spec:** [Consent panel design](../specs/2026-11-20-issue-1001-gui-phase-5-5-the-consent-panel-in-the-shells-ta-design.md).

## Inputs

Read the entire spec, its inline visual explanation, and the [full-page mockup](../mockups/2026-11-20-issue-1001-consent-panel.html). There is no approved parent spec or additional conceptual HTML companion. Inspected integration base: `0775639aa40617ea34928b2e82259f98fe6d3437` (`origin/pyside-gui`).

Read `AGENTS.md`, `gui/consent.py`, `gui/shells.py`, `gui/workspace.py`, `gui/bridge.py`, and their existing tests before changes. Here, `gui/` means `src/msks/msks/client/gui/` and `tests/` means `src/msks/tests/`.

## Global constraints

- The implementation PR must target `pyside-gui`.
- The panel is visible exactly when the active presenter's `rows()` is nonempty.
- `DURATION_DEFAULT` is `tilrestart`.
- Each duration menu uses `once`, `5m`, `15m`, `tilrestart`, `forever`, in that order.
- The panel must not remove a hold on either reply.
- The timer runs while Shells is visible, including while the panel is empty.
- Registration remains owned by the workspace page, including when another workspace tab is selected.
- Use platform fonts, palette, metrics, and menu behavior.
- Use plain text for destinations and errors.
- Preserve all terminal lifetimes and Qt-free CLI imports.
- Run toolchain commands through `devenv --quiet -O dotenv.enable:bool false shell --`.
- Do not commit or push implementation without the applicable user authorization. Planning's commit authorization does not authorize product commits.

## File responsibilities

| File                             | Change and responsibility                                                        |
| -------------------------------- | -------------------------------------------------------------------------------- |
| `gui/consentpanel.py`            | New panel: rows, explicit verdict controls, reply matching, flash lifetime       |
| `gui/consent.py`                 | Explicit re-export of existing duration constants for the widget                 |
| `gui/shells.py`                  | New `ShellsTab`: composition and visible-tab timer, existing hub/strip unchanged |
| `gui/workspace.py`               | Host the presenter and container, preserve `page.shells`, correct focus identity |
| `tests/test_gui_consentpanel.py` | New offscreen panel behavior tests, fake presenter and bridge                    |
| `tests/test_gui_consent.py`      | Assert unique keys without depending on the process-wide counter's initial value |
| `tests/test_gui_shells.py`       | Container, timer, focus, and terminal persistence tests                          |
| `tests/test_gui_workspace.py`    | Stack identity and presenter hosting tests                                       |
| `tests/test_gui_zoom.py`         | Exercise the real Shells container with holds through font-zoom changes          |
| `docs/changes.md`                | Unreleased Added entry for the user-visible feature                              |

Also modify `import-graph.toml` in Task 1 to declare `msks.client.gui.consent` as a deliberate duration-constant facade. Explain the shared vocabulary and unchanged TUI fan-in in its comment.

The panel is imported lazily through `shells.py`. Do not add it to `gui/__init__.py`, which runs at CLI parse time. Keep the new panel focused rather than expanding the workspace page toward the 1000-line ceiling.

## Review focus

Each condition has a test in the task that owns it.

1. A stopped bridge emits failure synchronously. The panel must show that failure and retire its key (Task 1).
2. A menu remains open across a countdown tick. The same request and controls must remain active (Task 1).
3. A workspace changes before a menu action or verdict reply arrives. The new presenter must remain unaffected (Tasks 1 and 3).
4. The final row disappears while its action holds focus. Focus must reach the terminal strip without ending a session (Tasks 2 and 3).
5. The Shells tab starts empty or becomes hidden. Empty queues must still detect new holds, and hidden tabs must stop their timer (Task 2).

## Verification commands

Run all commands from the assigned worktree. Use this prefix for every command below:

```bash
devenv --quiet -O dotenv.enable:bool false shell --
```

For targeted test cycles, append:

```bash
env QT_QPA_PLATFORM=offscreen python -m pytest -o addopts= -q <test-path>
```

`-o addopts=` avoids enforcing whole-package coverage on a targeted test. The final preflight provides the coverage gate. A red cycle must fail at the missing behavior, not a missing dependency or Qt setup error. Stop for guidance if a substantive existing defect blocks execution.

## Task 1: Render holds and dispatch explicit verdicts

**Files:** Create `gui/consentpanel.py` and `tests/test_gui_consentpanel.py`. Modify `gui/consent.py` to explicitly re-export duration constants without changing presenter behavior. Modify `tests/test_gui_consent.py` to remove its process-order dependency and `import-graph.toml` for the deliberate facade.

**Consumes:** `ConsentPresenter.rows()`, `decide(request_id, decision, duration)`, `decide_keys`, and `retire(key)`. Bridge signals are `succeeded(str, object)` and `failed(str, object)`.

**Produces:** `ConsentPanel(bridge, parent=None)`, `host(presenter: ConsentPresenter | None) -> None`, `refresh() -> None`, and the Qt signal `focus_exhausted()`.

- [ ] Add the existing Qt-availability guard and fixture pattern from `test_gui_workspace.py`. Pin `QT_QPA_PLATFORM=offscreen` before QApplication construction. Use a presenter fake with replaceable `ConsentRow` values and recorded decisions. Use a bridge stub with real Qt signals, no network or worker.
- [ ] Write failing rendering tests: `test_empty_queue_collapses_panel`, `test_rows_follow_presenter_order`, `test_refresh_updates_countdown_without_replacing_controls`, and `test_removed_row_sends_nothing`. Assert hidden state, visible destination/countdown text, stable action-widget identity, and no dispatch from a stale menu callback. Include markup-like host text and port-zero presenter labels without reformatting.
- [ ] Run the new test file. Confirm failure at the absent panel interface.
- [ ] Implement the panel with a compact heading, bounded scroll area, stable row widgets keyed by id, and native split actions. Set QLabel text format to PlainText. Hide the whole panel for an empty snapshot. Cap the row area's preferred height at approximately three rows, using font/style metrics rather than fixed text pixels.
- [ ] Write parameterized `test_main_action_uses_tilrestart` for both decisions and `test_duration_action_sends_immediately` for all ten decision/duration pairs. Assert exactly `(request_id, decision, duration)` per activation. Add `test_menu_cancel_sends_nothing` and `test_menu_choice_does_not_arm_next_click`. Drive actual buttons and QAction signals, not a duplicate dispatch helper.
- [ ] Implement QToolButton `MenuButtonPopup` controls. In `gui/consent.py`, explicitly re-export `DURATIONS` and `DURATION_DEFAULT` from the existing TUI consent import. Import these in the panel through that deliberate facade. This avoids another direct importer of the fan-in-limited TUI controller. Declare `msks.client.gui.consent` in `import-graph.toml`'s `facades` list with the reason stated above. Before dispatch, verify active presenter identity and current request membership. Menu activation and main activation must not both fire for one choice.
- [ ] Write reply tests with a real `ConsentPresenter` and stub bridge: `test_success_keeps_hold_until_resolved_frame`, `test_failed_verdict_flashes_and_retires_key`, `test_stopped_bridge_failure_is_matched_synchronously`, and `test_unrelated_and_old_keys_are_ignored`. Seed the real controller with request/resolved JSON frame shapes from `test_gui_consent.py`. Assert bridge call method `decide`, workspace id, request id, decision, and duration. Assert success leaves the row. Apply a resolved frame, refresh, and assert disappearance.
- [ ] Implement exact active-key matching and retirement. For failure, show `decide failed: {error}` for 4000 ms. Use a sequence and receiver-bound single-shot callback. Add `test_new_flash_survives_old_timeout`, `test_refresh_preserves_flash`, `test_empty_queue_hides_flash`, and `test_host_clears_flash_and_invalidates_old_actions`. Deliver captured timer callbacks deterministically, without sleeping four seconds.
- [ ] Add `test_narrow_panel_wraps_actions` and `test_large_queue_scrolls_without_consuming_terminal_area`. At a narrow width, assert action controls remain inside the panel and appear below destination/countdown labels. With five rows, assert a scrollbar exposes every action and the terminal retains visible usable space. Resize and scale the font without asserting fixed pixel values.
- [ ] Add `test_timer_refresh_preserves_open_menu_and_focus` through `refresh()` and actual controls. Add native keyboard activation/cancellation coverage with QTest. Ensure long labels can shrink without clipping actions and expose their full text accessibly.
- [ ] Update `tests/test_gui_consent.py::test_the_decide_call_sends_verdict_and_duration_together`. Capture generated keys instead of requiring `consent-decide-1`, `-2`, and `-3`. Preserve assertions for exact dispatch arguments, retirement, and distinct keys across flights and presenters. Keep the production counter unchanged.
- [ ] Run `tests/test_gui_consentpanel.py tests/test_gui_consent.py` in that explicit order with the targeted command. Expect all tests to pass without warnings. This pins independence from the process-wide sequence. Do not add document-string, palette-value, or widget-count-only tests.

## Task 2: Compose the panel above persistent terminal tabs

**Files:** Modify `gui/shells.py` and `tests/test_gui_shells.py`.

**Consumes:** Task 1's panel interface, existing `ShellTabs(hub)` and `ShellTabs.host(workspace_id)`, and `bridge.decider_landed(str, object)`.

**Produces:** `ShellsTab(hub, bridge)`, attributes `shells: ShellTabs`, `consent_panel: ConsentPanel`, and `host(workspace_id: str, presenter: ConsentPresenter) -> None`.

- [ ] Write `test_shells_container_places_consent_above_terminals`, `test_empty_panel_keeps_terminal_area`, and `test_container_timer_refreshes_countdown`. Use presenter fakes and the existing sleeper/stub-terminal fixture pattern. Assert geometry and visible behavior, not layout source text.
- [ ] Run those tests. Confirm the missing container fails them.
- [ ] Add `ShellsTab` with a zero-margin vertical layout, the panel above `ShellTabs`, and terminal stretch. Its parent-owned timer interval is 1000 ms. On show, refresh immediately and start the timer. On hide, stop it. Guard partially constructed show/hide events as existing screen code does.
- [ ] Connect decider notifications to snapshot refresh without interpreting payloads. Connect the timer once. `host` replaces the panel's presenter and hosts the strip's workspace. A notification while hidden can refresh state but must not restart the timer.
- [ ] Write `test_empty_visible_shells_detects_first_hold`, `test_hidden_shells_stops_timer_and_show_refreshes`, and `test_stale_notification_reads_only_active_presenter`. Emit the existing timer timeout and bridge signals deterministically. Change the fake clock and assert changed countdown text without real-time waits.
- [ ] Add `test_focused_row_removal_reaches_terminal_strip`: remove the focused request, refresh, and assert focus on the next row's Allow action. If the removed row was last, focus the preceding row. Remove the final request and assert focus on the current terminal or plus strip. Connect the panel's `focus_exhausted` signal to `ShellsTab.focus_shell() -> None` for that handoff. Assert a background resolution never steals terminal focus.
- [ ] Run the full `tests/test_gui_shells.py` and panel tests. Existing close, rename, plus-tab, and persistence behaviors must still pass without new warnings.

## Task 3: Host the panel through workspace navigation and validate delivery

**Files:** Modify `gui/workspace.py`, `tests/test_gui_workspace.py`, affected assertions in `tests/test_gui_shells.py` and `tests/test_gui_zoom.py`, and `docs/changes.md`.

**Consumes:** Task 2's container and the existing `WorkspaceDetail.consent` ownership and decider lifecycle.

**Produces:** `WorkspaceDetail.shells_tab`, preserved `WorkspaceDetail.shells`, and the completed user-visible feature.

- [ ] Update `test_the_page_carries_top_level_shells_and_info_tabs` to require `page.stack.widget(0) is page.shells_tab` and `page.shells_tab.shells is page.shells`. Write `test_open_hosts_consent_before_registration` with a stub registration that immediately applies a request frame and emits notification. Assert the correct row appears above the terminal.
- [ ] Run the affected workspace tests. Confirm failure at the new composition contract.
- [ ] Construct `ShellsTab` in the page and keep the existing `new_shell` connection through `page.shells`. Replace only the first stack entry. In `open()`, create `ConsentPresenter`, host it, preserve first-shell creation, and then call `start_decider` with its controller. Update `anchor_focus()` to compare against `shells_tab` while focusing `page.shells.currentWidget()` as before.
- [ ] Write `test_workspace_switch_discards_old_consent_actions_and_results` and `test_reopen_uses_fresh_presenter_without_closing_shells`. Seed different holds in two presenters. Deliver an old key and invoke an old menu action after switching. Assert no new-workspace call or flash. Assert Back and reopen preserve the hub's terminal object and fresh registration state.
- [ ] Add empty allow/static workspace cases and a controller-reset case to the integration tests. Preserve the rule that the panel only uses presenter rows, not mode. Add a visible-shells focus test and verify other workspace tabs do not stop the page's decider registration.
- [ ] Update affected shell/zoom tests for the container identity. Preserve assertions about shell creation, focused terminal, Escape, rename, close, zoom, Back, and hidden persistent sessions. Extend `tests/test_gui_zoom.py::test_the_grid_holds_through_the_windows_real_chrome` to use the real `ShellsTab` with pending rows. Verify its action geometry and terminal grid after grow/shrink/reset rather than relying on plain QTabWidget scaffolding.
- [ ] Add an Added entry under `## \[Unreleased]`: lead with **Shells consent panel (#1001).** Describe inline decisions, duration menus, and pending-request visibility in two short sentences.
- [ ] Run all `tests/test_gui_*.py` and `tests/test_client_gui.py` with the targeted command. Expect all tests to pass without new warnings.
- [ ] Run `msks-preflight`. Expect a green result for all Python gates, including lines, reachability, fan-in, coupling snapshot, complexity, and coverage. If an intentional new cross-package import needs a graph change, state its reason. Prefer using the existing consent import path over widening fan-in. Do not suppress coverage gaps or warnings from tests touched here.
- [ ] Run `uvx --from zensical==0.0.71 zensical build --strict` (the docs workflow's version). Expect a successful docs build. Inspect mockup and document links directly rather than adding tests for their text.
- [ ] Apply the sibling `patchmill-visual-evidence` and `patchmill-frontend-review` skills at their implementation boundaries. Capture actual widget evidence separately from the planning mockup. Do not start the development daemon without permission. If unavailable tooling blocks required evidence, ask the maintainer rather than claim success.
- [ ] Review the diff and run `git diff --check`. Confirm only intended files changed. Once implementation commit authorization exists, stage only those files and commit. Run any gates again if the tree changes. Patchmill owns push/publication and the implementation PR must contain `Closes #1001.` with base `pyside-gui`.

## Testing value gate

Automated tests target visible consent decisions, async reply ownership, countdowns, focus, and terminal persistence. These are maintained regression surfaces. Use existing controller tests for parsing, clock arithmetic, and destination formatting rather than duplicating them. Use direct inspection and the docs build for prose, links, and the mockup.

## Visual explanation

Each task produces a separately testable interface. Final integration preserves the presenter's state ownership and the hub's terminal ownership.

```text
Task 1: ConsentPanel
  fake rows → visible controls → real presenter + stub bridge replies
       ↓
Task 2: ShellsTab
  panel + existing ShellTabs + visible-tab timer → stable focus
       ↓
Task 3: WorkspaceDetail integration
  fresh presenter per open → host → register → preserve terminals
       ↓
  GUI regressions → preflight + docs → widget evidence → delivery review
```

## Open assumptions

None. If the integration base changes the presenter interface, stop and reconcile this plan before implementation. No daemon, policy, or registration redesign is authorized.

## Planning review

### Initial combined review and scoped confirmation

- Scope: this plan and `docs/specs/2026-11-20-issue-1001-gui-phase-5-5-the-consent-panel-in-the-shells-ta-design.md`, including both inline explanations and the mockup. The specification was in scope, not an approved read-only input.
- Reviewer: fresh-context canonical `reviewer`, model `openai-codex/gpt-6.1-sol`, thinking `xhigh`. Initial run `0606e793-c888-45d6-a7e9-0e87a1546b69`. Fresh confirmation run `e6e0be80-5b71-42af-85e8-fcadae962186` used the same model and thinking.
- Base: `0775639aa40617ea34928b2e82259f98fe6d3437` (`origin/pyside-gui`).
- Initial reviewed plan body SHA-256: `9ccaf75b97841311ece9caa5f06b119f1567917e2acca2336f4a9681a6e03c47`.
- Initial compared specification body SHA-256: `7fca1b996366148f8c9071d9957fede83d791b114f70042d6ebf22c4423e5e50`.
- Survey: independent inspection confirmed process-wide verdict keys, synchronous bridge failure, daemon-authoritative row removal, page registration, hub terminal ownership, and the existing offscreen test seams. The proposed interfaces preserve those responsibilities.
- B1, blocker, Accepted: Task 1 modifies the existing presenter verdict test to capture actual keys. It preserves argument, retirement, and cross-presenter uniqueness assertions and requires the panel-before-presenter run order.
- S1, suggestion, Accepted: use `WorkspaceDetail`, the actual source class, throughout both documents.
- S2, suggestion, Accepted: explicit narrow wrapping and five-row scrolling tests now accompany real `ShellsTab` composition under zoom/grid checks.
- S3, suggestion, Accepted: the linked mockup now uses top-layer duration popovers instead of menus clipped by the scroller.
- Limits: neither reviewer executed tests, widgets, browser rendering, docs builds, or gates. The first review used source tracing. The scoped confirmation independently inspected corrections, relevant source/tests, and raw Git metadata without entering the toolchain shell.
- Confirmation: B1 and S1–S3 are addressed. No remaining scoped blockers or new suggestions exist. The corrected plan retains all issue requirements and accurate inline task-order explanation.
- Confirmation reviewed plan body SHA-256: `4ee9d95ff1d5c4df8cc56000bd79874b1c7a10e2e4b6013e187d1263df6b8c36`. Confirmation compared specification body SHA-256: `664235c7982e6b531a13b2f2733a670a77e5057a7ae477dd35e4e82c1b74c272`.
- Final records add only a separator blank line before each review heading. No substantive body changed after confirmation.
- Final plan body SHA-256: `b81d31f6c1e610e2d51c78b774c13bea690a66db2c7fa87fe0984b9c4e96f561`.
- Final compared specification body SHA-256: `25793260730f7eb9df75d405c3d05361c5b4e00f06e4e7282211aa3ce2b960e3`.
- Outcome: `ready`. This record does not grant maintainer approval.
