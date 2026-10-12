const fs = require("node:fs");

const scenario = JSON.parse(
  fs.readFileSync(process.env.DYAD_BENCHMARK_CASE_FILE, "utf8"),
);

const turns = [
    {
      text: "I will write behavior-focused acceptance coverage.",
      toolCalls: [
        {
          name: "write_file",
          args: { path: scenario.testFile, content: scenario.testContent },
        },
      ],
    },
    {
      text: "I will run it against the seeded fault.",
      toolCalls: [
        {
          name: "run_tests",
          args: { testFile: scenario.testFile, grep: scenario.focusedGrep },
        },
      ],
    },
    {
      text: "I will inspect network and browser-console evidence.",
      toolCalls: [{ name: "read_logs", args: { type: "network-requests" } }],
    },
    {
      text: "I will inspect the browser console evidence.",
      toolCalls: [{ name: "read_logs", args: { type: "client" } }],
    },
    {
      text: "I will repair the failed application behavior.",
      toolCalls: [
        {
          name: "write_file",
          args: {
            path: "src/App.tsx",
            content: scenario.repairedNew,
          },
        },
      ],
    },
    {
      text: "I will rerun the focused behavior.",
      toolCalls: [
        {
          name: "run_tests",
          args: { testFile: scenario.testFile, grep: scenario.focusedGrep },
        },
      ],
    },
    {
      text: "I will now run the owning regression spec.",
      toolCalls: [{ name: "run_tests", args: { testFile: scenario.testFile } }],
      textAfterTools: "The evidence and traceability ledger is ready.",
    },
  ];

exports.fixture = {
  description: "Public acceptance fail-repair-focused-regression fixture",
  // The local-agent fake server can start a bounded follow-up pass after a
  // tool transcript normalization. Keep the deterministic sequence available
  // to those follow-ups instead of silently ending the acceptance run.
  passes: [{ turns }, { turns }, { turns }],
};
