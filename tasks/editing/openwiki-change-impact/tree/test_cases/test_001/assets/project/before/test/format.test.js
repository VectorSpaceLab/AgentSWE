import assert from "node:assert/strict";
import test from "node:test";
import { format } from "../src/format.js";

test("formats text", () => assert.equal(format("hi"), "HI"));
