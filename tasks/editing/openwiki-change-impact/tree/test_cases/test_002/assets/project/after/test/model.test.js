import assert from "node:assert/strict";
import test from "node:test";
import { makeUser } from "../src/model.js";
test("uses string ids", () => assert.equal(makeUser().id, "usr-7"));
