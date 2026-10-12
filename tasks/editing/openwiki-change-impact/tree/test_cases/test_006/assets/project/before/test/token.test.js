import assert from "node:assert/strict";
import test from "node:test";
import { normalizeToken } from "../src/token.js";
test("trims tokens", () => assert.equal(normalizeToken(" x "), "x"));
