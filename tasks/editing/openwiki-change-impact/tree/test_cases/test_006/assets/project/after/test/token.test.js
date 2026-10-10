import assert from "node:assert/strict";
import test from "node:test";
import { normalizeToken } from "../src/token.js";
test("trims tabs as well as spaces", () => assert.equal(normalizeToken("\tx\t"), "x"));
test("retains internal spaces", () => assert.equal(normalizeToken("x y"), "x y"));
