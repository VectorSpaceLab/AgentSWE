import assert from "node:assert/strict";
import test from "node:test";
import { mode } from "../packages/alpha/index.js";
test("alpha mode", () => assert.equal(mode(), "alpha-v1"));
