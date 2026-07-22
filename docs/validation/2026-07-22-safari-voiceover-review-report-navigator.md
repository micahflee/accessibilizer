# macOS Safari and VoiceOver interactive Review Report validation

Issue: #53 (parent #37)

Result: **PENDING — awaiting a recorded macOS Safari + VoiceOver session.**

> This document is a prepared session template. The interactive Review Report
> navigator was built and verified with the automated Node + Playwright suite
> (`tests/browser/`), but the WCAG 2.2 AA acceptance criterion for issue #53
> additionally requires a recorded manual pass with a real screen reader, which
> cannot be performed by the coding agent. Fill in the **Result** and each
> **Observation** cell below during a live session, then flip the top-level
> Result to `passed` (or record the failures and file follow-ups).

## Environment

- macOS <version> (build <build>)
- Safari <version> (build <build>)
- VoiceOver <version>
- Accessibilizer commit `<commit>` (branch `agent/interactive-review-report-navigator`)
- Review Report generated from: `<bundle or --source/--record>`
- Source PDF SHA-256: `<sha256>`

Generate the report to review with either:

```
accessibilizer report --bundle <bundle>.accessibilizer
# or, standalone:
accessibilizer report --source <source>.pdf --record <review-record>.yaml --output <dir>
```

Open `review-report.html` from the output directory in Safari (over `file://`,
with no network access) and drive it with VoiceOver (VO = Control+Option).

## Automated preconditions

- `cd tests/browser && npm install && npx playwright install chromium && npx playwright test` — all headless-browser tests pass (stepping, filtering, disclosures, URL/history, splitter, reflow, overlay alignment).
- `make test-cli` — the Python renderer and security/escaping tests pass.
- The output directory contains only relative local assets: `review-report.html`, `review-report.css`, `review-report.js`, and `regions/`.

## Recorded session

| Check | Result | Observation |
| --- | --- | --- |
| Landmarks & headings | | VO rotor lists the main landmark, the single H1, the Document details / Conversion Warnings disclosures, and the Component navigator heading. |
| Component stepping | | Previous/Next move through every Component in Logical Reading Order; the page image switches when the page changes. |
| Live announcements | | Each step announces `Component N of M, <type>, page <p>` through the polite live region. |
| Focus stability | | Focus stays on the activated Previous/Next button while stepping. |
| Toolbar arrow keys | | Left/Right Arrow step only while focus is inside the navigation toolbar; they do nothing elsewhere. |
| Focus component details | | The `Focus component details` control moves VO focus into the active Component panel. |
| Concise view | | Type, page, primary assistive-technology content, warning status, and Source Region count are announced without opening a disclosure. |
| All details disclosure | | The native `All details` disclosure exposes stable ID, exact coordinates, secondary fields, attached warnings, Recognition Candidates, and labelled crops; its open/closed state carries forward while stepping. |
| Source Region boxes | | The active Component's referenced boxes appear on the page, numbered `1..N`, with the same numbers beside the region controls; state is not conveyed by color alone. |
| Fit page / Zoom to component | | Fit page shows the whole page; Zoom to component enlarges and reveals the referenced region(s); manual zoom in/out works; the selected mode is preserved while stepping. |
| Region emphasis / zoom | | A region control emphasizes its matching box and can zoom to it. |
| Warnings only | | The `Warnings only` toggle restricts traversal to warned Components; the empty state appears when none match. |
| Warnings summary | | Document- and page-level warnings remain reachable in the always-open-able Conversion Warnings disclosure and never appear as fake Components; unresolved counts are visible. |
| URL / history | | The fragment tracks the active node; reload restores it; Back/Forward move between visited Components; node/page/warning links activate state rather than jumping to hidden content. |
| Splitter | | The pane divider is operable by pointer and by keyboard (Arrow/Home/End) as a separator, and `Reset pane sizes` restores the default. |
| Reflow at 400% | | At 400% browser zoom (and narrow widths) the layout becomes a single column with the page view before the Component details; DOM, visual, focus, and reading order agree; no content or control is obscured. |
| Reduced motion | | With Reduce Motion enabled, automatic zoom/pan is instant and other transitions are minimal. |
| No-JavaScript | | With JavaScript disabled, the `<noscript>` message clearly states that the interactive report requires JavaScript. |

## Decision point

<Summarize whether the recorded session passes the WCAG 2.2 AA acceptance gate
for issue #53. If any check fails, record the failure, the interface state that
produced it, and the follow-up issue.>
