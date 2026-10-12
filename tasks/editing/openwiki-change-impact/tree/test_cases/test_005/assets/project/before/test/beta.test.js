import assert from "node:assert/strict";
import test from "node:test";
import { mode } from "../packages/beta/index.js";
test("beta mode", () => assert.equal(mode(), "beta-v1"));
