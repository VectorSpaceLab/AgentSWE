import assert from "node:assert/strict";
import test from "node:test";
import { renderName } from "../src/names.mjs";

test("renders a name", () => assert.equal(renderName("ada"), "ADA"));
