// @ts-check
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

// Render the real renderer output into tests/browser/.fixture once before the
// suite. Using the actual Python renderer (never a hand-written HTML copy) keeps
// the browser assertions honest about what the report generator emits.
module.exports = async () => {
  const outDir = path.join(__dirname, ".fixture");
  fs.rmSync(outDir, { recursive: true, force: true });
  const builder = path.join(__dirname, "build_fixture.py");
  const runners = [
    ["uv", ["run", "python", builder, outDir]],
    ["python3", [builder, outDir]],
  ];
  let lastError;
  for (const [cmd, args] of runners) {
    try {
      execFileSync(cmd, args, { stdio: "inherit" });
      lastError = undefined;
      break;
    } catch (error) {
      lastError = error;
    }
  }
  if (lastError) {
    throw new Error(
      `Could not build the report fixture with uv or python3: ${lastError.message}`
    );
  }
  if (!fs.existsSync(path.join(outDir, "review-report.html"))) {
    throw new Error("Fixture build did not produce review-report.html");
  }
};
