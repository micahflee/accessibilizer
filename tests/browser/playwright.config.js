// @ts-check
const { defineConfig, devices } = require("@playwright/test");

// The fixture report is rendered once (see global-setup.js) into .fixture/ and
// every test loads it over file:// — the report must run offline with no server.
module.exports = defineConfig({
  testDir: __dirname,
  globalSetup: require.resolve("./global-setup.js"),
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  // On CI keep the line reporter for logs and also emit a self-contained HTML
  // report so a failed run can upload it as a diagnostic artifact.
  reporter: process.env.CI
    ? [["line"], ["html", { open: "never" }]]
    : "list",
  use: {
    // A desktop viewport wide enough for the two-pane layout by default; the
    // reflow test overrides it per-test.
    viewport: { width: 1200, height: 900 },
    // Keep a trace only for tests that fail so CI can attach it on failure
    // without paying the cost on the (usual) all-green run.
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
