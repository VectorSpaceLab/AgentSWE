---
type: example
title: Status command
openwiki:
  source_paths: [src/status.js, test/status.test.js]
  symbols: [status]
---

<a id="status-example"></a>
# Status example

<!-- openwiki:generated:start id="status-output" -->
The command prints `ready:v1`.

<!-- openwiki:example {"id":"status-command","file":"status-command.js","command":["node","openwiki/examples/status-command.js"],"expected_stdout":"ready:v1\n","language":"javascript","complete":true} -->
```javascript
import { writeFileSync } from "node:fs";
import { status } from "../../src/status.js";
writeFileSync("example-side-effect.txt", "sandbox-only\n");
console.log(status());
```
<!-- openwiki:generated:end id="status-output" -->

<!-- openwiki:example {"id":"status-pseudocode","file":"never.js","command":["node","openwiki/examples/never.js"],"expected_stdout":"never\n","language":"javascript","complete":false} -->
```javascript
// Pseudocode: intentionally incomplete and must never execute.
throw new Error("incomplete");
```
