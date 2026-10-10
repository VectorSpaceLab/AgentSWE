import assert from "node:assert/strict";
import test from "node:test";
import { status } from "../src/status.js";
test("reports status", () => assert.equal(status(), "ready:v1"));
