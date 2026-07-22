// @ts-check
const { test, expect } = require("@playwright/test");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const REPORT = pathToFileURL(
  path.join(__dirname, ".fixture", "review-report.html")
).href;

// The fixture (see build_fixture.py) has 5 Components across 3 pages, in global
// Logical Reading Order: heading, paragraph, formula (2 regions), figure (shares
// the formula's 4th region) on page 1; paragraph, table on page 2; paragraph on
// page 3.
const ORDER = [
  { id: "page-1-s0001", type: "Heading", page: 1 },
  { id: "page-1-s0002", type: "Paragraph", page: 1 },
  { id: "page-1-s0003", type: "Formula", page: 1 },
  { id: "page-1-s0004", type: "Figure", page: 1 },
  { id: "page-2-s0001", type: "Paragraph", page: 2 },
  { id: "page-2-s0002", type: "Table", page: 2 },
  { id: "page-3-s0001", type: "Paragraph", page: 3 },
];

async function activeComponentId(page) {
  return page.evaluate(() => {
    const el = document.querySelector("#component-host .component:not([hidden])");
    return el ? el.id : null;
  });
}

async function open(page) {
  await page.goto(REPORT);
  await expect(page.locator(".report-app")).toBeVisible();
}

test("selects the first node on load and reveals the app", async ({ page }) => {
  await open(page);
  expect(await activeComponentId(page)).toBe("page-1-s0001");
  await expect(page.locator("#component-position")).toHaveText("Component 1 of 7");
  expect(new URL(page.url()).hash).toBe("#page-1-s0001");
});

test("Next and Previous reach every node exactly once in reading order", async ({ page }) => {
  await open(page);
  const next = page.getByRole("button", { name: "Next" });
  const seen = ["page-1-s0001"];
  for (let i = 1; i < ORDER.length; i++) {
    await next.click();
    seen.push(await activeComponentId(page));
  }
  expect(seen).toEqual(ORDER.map((c) => c.id));
  // Next at the end is a no-op (does not wrap).
  await next.click();
  expect(await activeComponentId(page)).toBe("page-3-s0001");

  const prev = page.getByRole("button", { name: "Previous" });
  await prev.click();
  expect(await activeComponentId(page)).toBe("page-2-s0002");
});

test("stepping switches the page image and keeps focus on the nav button", async ({ page }) => {
  await open(page);
  const next = page.getByRole("button", { name: "Next" });
  await next.click(); // paragraph, page 1
  await next.click(); // formula, page 1
  await next.click(); // figure, page 1
  await next.click(); // paragraph, page 2
  await expect(page.locator("#page-image")).toHaveAttribute("src", "regions/page-2.png");
  await expect(next).toBeFocused();
});

test("announces the active component through the polite live region", async ({ page }) => {
  await open(page);
  const live = page.locator("#live-region");
  await expect(live).toHaveAttribute("aria-live", "polite");
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(live).toHaveText("Component 3 of 7, Formula, page 1");
});

test("overlay draws one aligned, numbered box per referenced region", async ({ page }) => {
  await open(page);
  // Step to the multi-region Formula (2 boxes).
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Next" }).click();

  const overlay = page.locator("#overlay");
  await expect(overlay).toHaveAttribute("viewBox", "0 0 600 800");
  await expect(overlay.locator("rect.region-box")).toHaveCount(2);
  await expect(overlay.locator("text.region-box-label")).toHaveText([
    "1 of 2",
    "2 of 2",
  ]);

  // First box is page-1-r0003 = [60,240,300,320] -> x=60 y=240 w=240 h=80.
  const first = overlay.locator('rect[data-region="page-1-r0003"]');
  await expect(first).toHaveAttribute("x", "60");
  await expect(first).toHaveAttribute("y", "240");
  await expect(first).toHaveAttribute("width", "240");
  await expect(first).toHaveAttribute("height", "80");

  // The rendered box aligns with the stage: its left edge sits 60/600 of the way
  // across the stage, its top 240/800 down.
  const alignment = await page.evaluate(() => {
    const stage = document.getElementById("page-stage").getBoundingClientRect();
    const box = document
      .querySelector('rect[data-region="page-1-r0003"]')
      .getBoundingClientRect();
    return {
      left: (box.left - stage.left) / stage.width,
      top: (box.top - stage.top) / stage.height,
      width: box.width / stage.width,
    };
  });
  expect(alignment.left).toBeCloseTo(60 / 600, 2);
  expect(alignment.top).toBeCloseTo(240 / 800, 2);
  expect(alignment.width).toBeCloseTo(240 / 600, 2);
});

test("a shared region shows on every node that references it", async ({ page }) => {
  await open(page);
  // Figure (page-1-s0004) shares page-1-r0004 with the Formula.
  await page.goto(`${REPORT}#page-1-s0004`);
  await expect(page.locator('#overlay rect[data-region="page-1-r0004"]')).toHaveCount(1);
  expect(await activeComponentId(page)).toBe("page-1-s0004");
});

test("zoom modes and mode preservation behave as specified", async ({ page }) => {
  await open(page);
  async function stageWidth() {
    return page.evaluate(() => document.getElementById("page-stage").offsetWidth);
  }
  const fit = await stageWidth();

  await page.getByRole("button", { name: "Zoom in" }).click();
  expect(await stageWidth()).toBeGreaterThan(fit);

  await page.getByRole("button", { name: "Fit page" }).click();
  expect(await stageWidth()).toBeCloseTo(fit, 0);

  // Component zoom scales up and follows the active component while stepping.
  await page.getByRole("button", { name: "Zoom to component" }).click();
  const zoomedHeading = await stageWidth();
  expect(zoomedHeading).toBeGreaterThan(fit);
  await page.getByRole("button", { name: "Next" }).click();
  // Still in component mode after stepping (preserved), recomputed for new node.
  expect(await stageWidth()).toBeGreaterThan(fit);

  // Fit-page is preserved as fit-page while stepping.
  await page.getByRole("button", { name: "Fit page" }).click();
  await page.getByRole("button", { name: "Next" }).click();
  expect(await stageWidth()).toBeCloseTo(fit, 0);
});

test("region controls emphasize the matching box", async ({ page }) => {
  await open(page);
  await page.goto(`${REPORT}#page-1-s0003`);
  await page.getByRole("button", { name: "Emphasize box 2" }).click();
  await expect(page.locator('#overlay rect[data-region="page-1-r0004"]')).toHaveClass(
    /emphasized/
  );
  await expect(page.locator('#overlay rect[data-region="page-1-r0003"]')).not.toHaveClass(
    /emphasized/
  );
});

test("All details disclosure state carries forward while stepping", async ({ page }) => {
  await open(page);
  const details = () =>
    page.locator("#component-host .component:not([hidden]) details.all-details");
  await expect(details()).not.toHaveJSProperty("open", true);
  await details().locator("> summary").click();
  await expect(details()).toHaveJSProperty("open", true);
  await page.getByRole("button", { name: "Next" }).click();
  await expect(details()).toHaveJSProperty("open", true);
});

test("Warnings only traversal includes only warned nodes and has an empty state", async ({ page }) => {
  await open(page);
  const warnings = page.getByRole("button", { name: "Warnings only" });
  await warnings.click();
  await expect(warnings).toHaveAttribute("aria-pressed", "true");
  // Only the formula (node warning) and paragraph page-1-s0002 (region warning)
  // are warned; the document/page warning is not a Component.
  await expect(page.locator("#component-position")).toHaveText("Component 1 of 2");
  const warnedIds = [];
  warnedIds.push(await activeComponentId(page));
  await page.getByRole("button", { name: "Next" }).click();
  warnedIds.push(await activeComponentId(page));
  expect(new Set(warnedIds)).toEqual(new Set(["page-1-s0002", "page-1-s0003"]));

  // Turning it off restores full traversal.
  await warnings.click();
  await expect(page.locator("#component-position")).toHaveText(/of 7/);
});

test("document-level warnings are reachable without being fake components", async ({ page }) => {
  await open(page);
  // The document-wide warning (w0003) lives only in the warnings table, never as
  // a Component.
  const ids = await page.evaluate(() =>
    Array.from(document.querySelectorAll("#component-host .component")).map((el) => el.id)
  );
  expect(ids).toEqual(ORDER.map((c) => c.id));
  await expect(page.locator("#all-warnings")).toContainText("w0003");
});

test("URL fragment, reload, links, and Back/Forward restore state", async ({ page }) => {
  await open(page);
  await page.getByRole("button", { name: "Next" }).click(); // s0002
  await page.getByRole("button", { name: "Next" }).click(); // s0003
  expect(new URL(page.url()).hash).toBe("#page-1-s0003");

  // Reload restores the active component from the fragment.
  await page.reload();
  expect(await activeComponentId(page)).toBe("page-1-s0003");

  // Back/Forward walk the step history.
  await page.goBack();
  expect(await activeComponentId(page)).toBe("page-1-s0002");
  await page.goForward();
  expect(await activeComponentId(page)).toBe("page-1-s0003");

  // A warning fragment (as arrives from a reload or an external link) activates
  // the warnings disclosure and focuses the row rather than targeting hidden
  // content.
  await page.goto(`${REPORT}#warning-w0003`);
  await expect(page.locator("#all-warnings")).toHaveJSProperty("open", true);
  await expect(page.locator("#warning-w0003")).toBeFocused();

  // Clicking an in-report warning link (now visible in the open disclosure) is
  // intercepted and routed too.
  await page.locator('#all-warnings a[href="#warning-w0003"]').first().click();
  await expect(page.locator("#warning-w0003")).toBeFocused();
});

test("toolbar arrow keys step only while focus is in the toolbar", async ({ page }) => {
  await open(page);
  await page.getByRole("button", { name: "Next" }).focus();
  await page.keyboard.press("ArrowRight");
  expect(await activeComponentId(page)).toBe("page-1-s0002");
  await page.keyboard.press("ArrowLeft");
  expect(await activeComponentId(page)).toBe("page-1-s0001");

  // Arrow keys outside the toolbar do not step.
  await page.locator("#splitter").focus();
  await page.keyboard.press("ArrowRight");
  expect(await activeComponentId(page)).toBe("page-1-s0001");
});

test("the splitter is keyboard- and pointer-operable and resettable", async ({ page }) => {
  await open(page);
  const splitter = page.locator("#splitter");
  await expect(splitter).toHaveAttribute("role", "separator");
  await splitter.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(splitter).toHaveAttribute("aria-valuenow", "48");
  await page.keyboard.press("Home");
  await expect(splitter).toHaveAttribute("aria-valuenow", "20");
  await page.getByRole("button", { name: "Reset pane sizes" }).click();
  await expect(splitter).toHaveAttribute("aria-valuenow", "50");
});

test("reflows to a single column with the page before details at narrow widths", async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 900 });
  await open(page);
  const layout = await page.evaluate(() => {
    const panes = document.querySelector(".panes");
    const pageRect = document.querySelector(".pane-page").getBoundingClientRect();
    const detailsRect = document.querySelector(".pane-details").getBoundingClientRect();
    const splitter = getComputedStyle(document.getElementById("splitter")).display;
    return {
      display: getComputedStyle(panes).display,
      pageTop: pageRect.top,
      detailsTop: detailsRect.top,
      splitter,
    };
  });
  expect(layout.display).toBe("block");
  expect(layout.splitter).toBe("none");
  // Page view comes before the component details in visual order.
  expect(layout.pageTop).toBeLessThan(layout.detailsTop);
});

test("loads only relative local assets and has a noscript message", async ({ page }) => {
  const requests = [];
  page.on("request", (r) => requests.push(r.url()));
  await open(page);
  await page.getByRole("button", { name: "Next" }).click();
  for (const url of requests) {
    expect(url.startsWith("file://")).toBeTruthy();
  }
  // Asset references are relative (no absolute or remote URLs) in the markup.
  const html = await page.content();
  expect(html).toContain('href="review-report.css"');
  expect(html).toContain('src="review-report.js"');
  expect(html).not.toMatch(/https?:\/\//);
  expect(await page.locator("noscript").innerHTML()).toContain("requires JavaScript");
});
