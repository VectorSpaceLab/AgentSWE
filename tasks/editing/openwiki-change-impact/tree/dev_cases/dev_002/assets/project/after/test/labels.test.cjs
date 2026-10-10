const assert = require("node:assert/strict");
const test = require("node:test");
const { label } = require("../src/labels.cjs");

test("uses the default separator", () => assert.equal(label("Ada"), "Ada -> Admin"));
test("accepts an explicit separator", () => assert.equal(label("Ada", { separator: " /" }), "Ada / Admin"));
