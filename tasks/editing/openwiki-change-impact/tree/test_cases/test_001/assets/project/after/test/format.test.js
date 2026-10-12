import assert from "node:assert/strict";
import test from "node:test";
import { format as current } from "../src/text/format.js";
import { format as compatible } from "../src/format.js";

test("new path formats text", () => assert.equal(current("hi"), "HI"));
test("compatibility path works", () => assert.equal(compatible("hi"), "HI"));
