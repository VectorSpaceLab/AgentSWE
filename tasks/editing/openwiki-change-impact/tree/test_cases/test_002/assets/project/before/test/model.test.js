import assert from "node:assert/strict";
import test from "node:test";
import { makeUser } from "../src/model.js";
test("uses numeric ids", () => assert.equal(makeUser().id, 7));
