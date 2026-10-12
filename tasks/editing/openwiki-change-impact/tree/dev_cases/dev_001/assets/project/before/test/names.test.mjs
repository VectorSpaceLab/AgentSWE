import assert from "node:assert/strict";
import test from "node:test";
import { formatName } from "../src/names.mjs";

test("formats a name", () => assert.equal(formatName("ada"), "ADA"));
