import assert from "node:assert/strict";
import test from "node:test";
import { fetchRecord, getRecord } from "../src/read.js";
test("replacement and deprecated alias work", () => {
  assert.equal(getRecord(42), "record:42");
  assert.equal(fetchRecord(42), "record:42");
});
