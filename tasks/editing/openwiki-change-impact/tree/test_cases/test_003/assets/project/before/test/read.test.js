import assert from "node:assert/strict";
import test from "node:test";
import { fetchRecord, legacyFetch } from "../src/read.js";
test("both readers work", () => {
  assert.equal(legacyFetch(42), "record:42");
  assert.equal(fetchRecord(42), "record:42");
});
