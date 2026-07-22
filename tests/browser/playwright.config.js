// @ts-check
const { defineConfig, devices } = require("@playwright/test");

// The fixture report is rendered once (see global-setup.js) into .fixture/ and
// every test loads it over file:// — the report must run offline with no server.
module.exports = defineConfig({
  testDir: __dirname,
  globalSetup: require.resolve("./global-setup.js"),
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? "line" : "list",
  use: {
    // A desktop viewport wide enough for the two-pane layout by default; the
    // reflow test overrides it per-test.
    viewport: { width: 1200, height: 900 },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
