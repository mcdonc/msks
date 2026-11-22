# Shells consent panel (#1001)

## Purpose and scope

The workspace operator answers held egress requests without leaving the terminal. A consent panel appears above the terminal tabs inside Shells. This delivers the panel portion of GUI phase 5.5 (#924).

The panel shows pending requests, countdowns, Allow, Deny, and duration menus. It reports verdict failures in place. The presenter, decider registration, daemon policy, and terminal ownership remain unchanged. Policy editing, verdict history, bulk decisions, and automatic tab switching are outside this issue.

## Inputs and current behavior

Inspected base: `origin/pyside-gui`, `0775639aa40617ea34928b2e82259f98fe6d3437`. The maintainer authorized rebasing the assigned branch from `main`. The implementation PR must target `pyside-gui`.

- `src/msks/msks/client/gui/consent.py`: `ConsentPresenter` owns the shared controller. `rows()` returns immutable `ConsentRow(id, dest, remaining, countdown)` values, oldest first. `decide(request_id, decision, duration)` records a unique key before calling the bridge. `decide_keys` and `retire(key)` support reply attribution.
- `gui/decider.py`: `DeciderLink` applies frames to that controller on the worker thread. Re-registration resets its pending snapshot. `SharedController` protects row snapshots with a lock.
- `gui/bridge.py`: `decider_landed(outcome, payload)` announces applied frames. Verdict replies use `succeeded(key, result)` or `failed(key, error)`. A stopped bridge can emit failure synchronously.
- `gui/workspace.py`: `WorkspaceDetail.open()` creates a new presenter at every open. The page starts and stops registration on show and hide. Its Shells stack entry is currently `ShellTabs`. `anchor_focus()` depends on that entry's identity.
- `gui/shells.py`: `ShellHub` owns persistent terminals. `ShellTabs` hosts them without ending sessions on page changes.
- `client/tui/consent.py`: `DURATIONS` is `once`, `5m`, `15m`, `tilrestart`, `forever`. `DURATION_DEFAULT` is `tilrestart`.
- `client/data.py::RestData.decide` sends the REST verdict. `server/api/egress.py::decide_egress` returns a verdict or an HTTP refusal. Refusals reach widgets through `failed`, not a boolean result.
- Existing evidence: `test_gui_consent.py`, `test_gui_decider.py`, `test_gui_workspace.py`, `test_gui_shells.py`, and `test_gui_zoom.py`.

Source paths abbreviated as `gui/` are relative to `src/msks/msks/client/`.

## Approaches

Use an ordinary QWidget section in a Shells container. Keep `ShellTabs` as the terminal strip. This preserves the hub's lifetime and keeps consent controls independent of terminal sessions.

Putting the panel inside each terminal duplicates controls and ties consent to individual sessions. A dialog or separate window conflicts with the issue. Adding rendering to the pure presenter breaks its Qt-free interface. These alternatives do not fit the required behavior.

## Domain and architecture

This is a GUI adapter addition within the existing workspace egress-consent context. It changes composition, not domain relationships. A hold is a pending request. A verdict is an explicit Allow or Deny with a duration. A successful REST reply is not a resolved frame. These meanings already exist in the controller and issue.

No glossary, context map, legacy context document, or ADR exists in this checkout. No new domain term or costly architectural choice needs a companion document. Domain modeling therefore requires no canonical vocabulary change. Codebase design applies because the change adds a widget interface and a composition module.

### Ownership and interfaces

Create `gui/consentpanel.py` with `ConsentPanel(bridge, parent=None)`, a QWidget. Its caller interface is `host(presenter: ConsentPresenter | None) -> None` and `refresh() -> None`. The bridge enters through construction. The presenter enters through `host`. The panel never constructs a presenter or controller.

The panel owns row widgets, action menus, failure text, and verdict reply matching. Its `focus_exhausted` Qt signal asks the Shells container to restore terminal focus when the last focused row disappears. It connects to bridge result signals once. `host` replaces the active presenter, clears row widgets and flash state, and invalidates old flash callbacks. Old verdicts can complete, but their keys cannot affect the replacement presenter. Hiding Shells does not cancel a verdict already sent.

Add `ShellsTab(hub, bridge)` in `gui/shells.py`. It owns `shells: ShellTabs`, `consent_panel: ConsentPanel`, and a one-second QTimer. Its interface is `host(workspace_id: str, presenter: ConsentPresenter) -> None`. Its vertical layout puts the panel first and the terminal strip second with stretch. Outer margins and spacing are zero when the panel is hidden.

`ShellsTab` refreshes the panel on `bridge.decider_landed`, on show, and on each timer tick. These signals are notifications only. The panel always reads the currently hosted presenter's snapshot, never reapplies frame payloads. A stale notification cannot restore old rows. The timer runs while Shells is visible, including while the panel is empty. It stops on hide. Registration remains owned by the workspace page, including when another workspace tab is selected.

`WorkspaceDetail` owns `shells_tab: ShellsTab`. Preserve `page.shells` as an alias to its `ShellTabs` for existing shell actions. Replace the first stack entry with `shells_tab`. Update `anchor_focus()` to compare against the container. At every `open`, construct the presenter, host it with the workspace, then start registration with its controller. Host before registration to handle immediate frames. Preserve automatic first-shell creation and all hub lifetimes.

### Depth, seams, and dependencies

The presenter remains the deep module for hold state, destination formatting, countdown math, and verdict dispatch. The widget is intentionally a thin GUI adapter, as the issue requires. It adds useful locality for menus, reply matching, and row lifetimes rather than another pass-through presenter.

The existing presenter interface is the external seam. Production uses `ConsentPresenter`; widget tests use a small presenter fake. The existing bridge seam has the production worker and a signal-emitting test adapter. Qt is an in-process dependency. The daemon is a remote owned dependency already hidden behind the injected bridge. No additional transport interface or test-only production hook is required.

Deleting the panel would move menu semantics, stable row updates, and flash ownership into callers. Keeping them together provides leverage through two small methods. The panel hides widget construction, layout, and callback bookkeeping. Tests exercise its visible controls and existing signal interfaces.

## Behavior

### Visibility and countdown

The panel is visible exactly when the active presenter's `rows()` is nonempty. An empty queue hides the whole section, including its title and message. Empty queues in `allow` and `static` mode therefore consume no space. The panel does not read controller mode or reinterpret policy. After a mode change, the controller and daemon remain the source of pending holds.

Each row shows `row.dest` and `row.countdown`. The presenter already formats port zero as `(all ports)`. The panel preserves this label and the presenter's compact countdown. Time reaching zero does not remove a row. Only the controller's next snapshot removes it after resolution or registration reset.

Update existing rows by request id. Update countdown labels without rebuilding action controls. Preserve focus and an open duration menu across timer ticks. Remove widgets when their id disappears. Present rows in presenter order. Use a bounded, vertically scrollable row area when the queue exceeds about three rows, leaving the terminal usable.

### Verdict controls

Each row has two native QToolButtons with `MenuButtonPopup`: Allow and Deny. Clicking the main button sends `allow` or `deny` with `tilrestart`. A menu beside each action lists `once`, `5m`, `15m`, `tilrestart`, and `forever` in that order. Activating a menu item immediately sends that action with that duration.

Opening or cancelling a menu sends nothing. Selecting a duration does not arm the main button or another row. A later main click still sends `tilrestart`. No verdict control becomes a default button. Focus, row selection, and a timer tick never send a verdict. Native keyboard activation of the explicit action remains available.

Before dispatch, verify that the id still appears in the active presenter's rows. A menu callback for a resolved row sends nothing. Bind callbacks to the current presenter and request identity so a workspace switch cannot send an old choice through a new presenter. Repeated explicit actions can send separate verdicts. Each reply uses its own presenter's unique key.

### Replies and failure flash

For each result signal, test exact membership in the active presenter's `decide_keys`. Ignore other keys, including old presenters' keys. Retire a matching key on success or failure. A success changes no rows. A failure shows `decide failed: {error}` in the panel for 4000 ms, matching the existing GUI flash duration and TUI wording.

Use plain text for destinations and errors. New flashes invalidate earlier clear callbacks. Use a receiver-bound single-shot timer so deletion cannot invoke destroyed widgets. Refreshing countdowns does not erase a flash. If the queue becomes empty, hide the whole panel even if a flash remains. A fresh host clears that message.

Match replies even when `bridge.call` emits failure synchronously. Do not wait for the returned future to establish ownership. `ConsentPresenter.decide` already inserts the key first. The panel must not remove a hold on either reply. Registration refusals retain the existing workspace status-line path.

## Frontend design

Frontend design applies to layout, action menus, focus, and error feedback. The existing native Qt design takes priority over generic visual novelty. Use platform fonts, palette, metrics, and menu behavior. Add a compact `Pending connections` heading. Align destinations left, countdowns in a narrow trailing column, and actions at the right. Keep Allow and Deny visually equal. Color must not be the only distinction.

The mockup uses representative native-light tokens: background `#ffffff`, section `#f3f3f3`, text `#202020`, border `#767676`, focus `#005a9e`, and error `#a4262c`. These tokens explain hierarchy, not a new application stylesheet. Use a system sans-serif font for chrome and the existing terminal font for terminal content.

Keep the row area compact on desktop. At narrow widths, wrap actions below the destination and countdown instead of clipping them. Long destinations remain available through selectable text or a full tooltip. The section scrolls vertically for large queues. Accessible names distinguish action, destination, and duration. Tab and Shift-Tab reach all controls. Native menu arrow navigation and Escape cancellation remain intact. Countdown updates and new rows never steal terminal focus. If a focused row disappears, focus the next surviving row's Allow button, or the preceding row when the removed row was last. If none survives, emit `focus_exhausted`. The container focuses the current terminal, or the plus tab bar when no terminal exists.

The plain, restrained section matches the operator's terminal workflow. A redesigned page, accent-heavy banner, or animation would distract from the pending decision. The mockup includes full workspace context, a narrow layout, explicit menu actions, a failure state, and an empty state.

## Visual explanation

Only daemon state changes remove rows. The widget forwards explicit decisions through the existing presenter.

```text
WorkspaceDetail
  owns ConsentPresenter ── controller ── DeciderLink (worker)
  owns ShellsTab
    ConsentPanel reads presenter.rows()
      Allow / Deny / duration → presenter.decide() → bridge.call()
      succeeded(key) → retire key, keep row
      failed(key)    → retire key, flash, keep row
    ShellTabs → ShellHub's persistent terminals

resolved frame → controller removes hold → refresh → row disappears
Shells visible → 1-second timer → refresh → countdown labels update
```

## Verification and acceptance

Use offscreen widget tests on Linux and macOS, without a daemon, SSH, or KVM.

| Requirement                             | Verification                                                                          |
| --------------------------------------- | ------------------------------------------------------------------------------------- |
| Held flow above terminal tabs           | Presenter fake rows plus workspace composition test                                   |
| Allow and Deny with same-click duration | Both main actions and all ten menu actions assert exact dispatch arguments            |
| No armed state                          | Menu open/cancel sends nothing, menu action then main click resets to `tilrestart`    |
| Removal only on resolved state          | Stub bridge success leaves row, controller resolved frame removes it                  |
| Failed verdict flash                    | Matching bridge failure, synchronous failure, overlapping flash timeouts              |
| Empty panel                             | Empty fake, allow/static with empty queues, last resolved hold, presenter reset       |
| Timer and focus                         | Pinned presenter clock, timer signal, stable controls, show/hide, focused-row removal |
| Stale actions and results               | Workspace switch, stale menu action, old verdict key, unrelated reply                 |
| Existing shell behavior                 | Workspace, shells, zoom, and CLI import suites                                        |
| Project gates                           | `msks-preflight`, including lines, import graph, coverage, plus docs build            |

The implementation adds an Unreleased Added entry for the consent panel (#1001). Tests cover behavior, not document text or static style values. Implementation screenshots remain separate from this planning mockup.

## Companion artifacts

- `docs/mockups/2026-11-20-issue-1001-consent-panel.html`: [Workspace mockup](../mockups/2026-11-20-issue-1001-consent-panel.html).

## Open assumptions

None. Minor widget composition and lifecycle choices are planner decisions described above. The issue fixes the user-facing actions and duration semantics.

## Planning review

### Initial combined review and scoped confirmation

- Scope: this specification and `docs/plans/2026-11-20-issue-1001-gui-phase-5-5-the-consent-panel-in-the-shells-ta-plan.md`, including both inline explanations and the mockup.
- Reviewer: fresh-context canonical `reviewer`, model `openai-codex/gpt-6.1-sol`, thinking `xhigh`. Initial run `0606e793-c888-45d6-a7e9-0e87a1546b69`. Fresh confirmation run `e6e0be80-5b71-42af-85e8-fcadae962186` used the same model and thinking.
- Base: `0775639aa40617ea34928b2e82259f98fe6d3437` (`origin/pyside-gui`).
- Initial reviewed specification body SHA-256: `7fca1b996366148f8c9071d9957fede83d791b114f70042d6ebf22c4423e5e50`.
- Initial reviewed plan body SHA-256: `9ccaf75b97841311ece9caa5f06b119f1567917e2acca2336f4a9681a6e03c47`.
- Survey: the reviewer independently inspected presenter/controller snapshots, frame application, bridge dispatch, workspace composition, shell ownership, import gates, and existing tests. The existing seams support a thin Qt adapter. No glossary or ADR conflict exists.
- B1, plan blocker, Accepted: Task 1 now removes the existing verdict-key test's dependency on initial process-wide counter values. It verifies panel-before-presenter test order without changing production uniqueness.
- S1, suggestion, Accepted: both documents now use the actual `WorkspaceDetail` class name. Production symbols remain unchanged.
- S2, suggestion, Accepted: the plan now requires narrow-layout, five-row scrolling, and real-container zoom/grid assertions.
- S3, suggestion, Accepted: the mockup now uses top-layer popovers so the row scroller cannot clip its duration menus.
- Limits: the initial review used source inspection rather than test execution. The confirmation used read-only source and raw Git metadata. Neither reviewer ran widgets, browser rendering, docs builds, or gates. These are planning assessments, not implemented behavior claims.
- Confirmation: all four corrections are addressed. No scoped blockers or additional suggestions remain. Both inline explanations remain accurate and companion links resolve.
- Confirmation reviewed specification body SHA-256: `664235c7982e6b531a13b2f2733a670a77e5057a7ae477dd35e4e82c1b74c272`. Confirmation reviewed plan body SHA-256: `4ee9d95ff1d5c4df8cc56000bd79874b1c7a10e2e4b6013e187d1263df6b8c36`.
- Final records add only a separator blank line before each review heading. No substantive body changed after confirmation.
- Final specification body SHA-256: `25793260730f7eb9df75d405c3d05361c5b4e00f06e4e7282211aa3ce2b960e3`.
- Final plan body SHA-256: `b81d31f6c1e610e2d51c78b774c13bea690a66db2c7fa87fe0984b9c4e96f561`.
- Outcome: `ready`. This record does not grant maintainer approval.
