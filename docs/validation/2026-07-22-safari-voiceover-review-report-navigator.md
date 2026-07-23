# macOS Safari and VoiceOver interactive Review Report validation

Issue: #53 (parent #37)

Result: **PASSED — the interactive Review Report passed the recorded macOS
Safari and VoiceOver session.**

## Environment

- macOS 26.5.2 (build 25F84)
- Safari 26.5.2 (build 21624.2.5.11.8)
- VoiceOver 10 (build 993)
- Accessibilizer commit `f89e4ca0d977ed51bd43abf7775b696ac64bba1d` (branch `docs/record-safari-voiceover-validation`)
- Review Report generated from: Source PDF `testdata/Chapter 20_ Electric Current Resistance and Ohms Law.pdf` and Review Record `testdata/gold-review-record.yaml`
- Reviewed output directory: `~/Desktop/accessibilizer-gold-review`
- Source PDF SHA-256: `fe203e79ddc803a7eaa4e401222a186b861c6b04ed632c865a2eaad858f2c077`

Open `review-report.html` from the output directory in Safari (over `file://`,
with no network access) and drive it with VoiceOver (VO = Control+Option).

## Automated preconditions

- **Pass:** `cd tests/browser && npm install && npx playwright install chromium && npx playwright test` — all headless-browser tests passed (stepping, filtering, disclosures, URL/history, splitter, reflow, and overlay alignment).
- **Pass:** `make test-cli` — the Python renderer and security/escaping tests passed.
- **Pass:** The output directory contained only the relative local assets `review-report.html`, `review-report.css`, `review-report.js`, and `regions/`.

## Recorded session

| Check | Result | Observation |
| --- | --- | --- |
| Landmarks & headings | Pass | The VO rotor listed the main landmark, the single H1, the Document details / Conversion Warnings disclosures, and the Component navigator heading. |
| Component stepping | Pass | Previous/Next moved through every Component in Logical Reading Order; the page image switched when the page changed. |
| Live announcements | Pass | Each step announced `Component N of M, <type>, page <p>` through the polite live region. |
| Focus stability | Pass | Focus stayed on the activated Previous/Next button while stepping. |
| Toolbar arrow keys | Pass | Left/Right Arrow stepped only while focus was inside the navigation toolbar; they did nothing elsewhere. |
| Focus component details | Pass | The `Focus component details` control moved VO focus into the active Component panel. |
| Concise view | Pass | Type, page, primary assistive-technology content, warning status, and Source Region count were announced without opening a disclosure. |
| All details disclosure | Pass | The native `All details` disclosure exposed the stable ID, exact coordinates, secondary fields, attached warnings, Recognition Candidates, and labelled crops; its open/closed state carried forward while stepping. |
| Source Region boxes | Pass | The active Component's referenced boxes appeared on the page, numbered `1..N`, with the same numbers beside the region controls; state was not conveyed by color alone. |
| Fit page / Zoom to component | Pass | Fit page showed the whole page; Zoom to component enlarged and revealed the referenced region(s); manual zoom in/out worked; the selected mode was preserved while stepping. |
| Region emphasis / zoom | Pass | A region control emphasized its matching box and zoomed to it. |
| Warnings only | Pass | The `Warnings only` toggle restricted traversal to warned Components; the empty state appeared when none matched. |
| Warnings summary | Pass | Document- and page-level warnings remained reachable in the Conversion Warnings disclosure and never appeared as fake Components; unresolved counts were visible. |
| URL / history | Pass | The fragment tracked the active node; reload restored it; Back/Forward moved between visited Components; node/page/warning links activated state rather than jumping to hidden content. |
| Splitter | Pass | The pane divider was operable by pointer and by keyboard (Arrow/Home/End) as a separator, and `Reset pane sizes` restored the default. |
| Reflow at 400% | Pass | At 400% browser zoom, the layout became a single column with the page view before the Component details; DOM, visual, focus, and reading order agreed; no content or control was obscured. |
| Reduced motion | Pass | With Reduce Motion enabled, automatic zoom/pan was instant and other transitions were minimal. |
| No-JavaScript | Pass | With JavaScript disabled, the `<noscript>` message clearly stated that the interactive report requires JavaScript. |

## Decision point

The automated renderer/security and interactive browser suites passed, and the
recorded macOS Safari and VoiceOver session passed every manual check. The
Review Report therefore passes the WCAG 2.2 AA acceptance gate for issue #53
and its parent issue #37.
