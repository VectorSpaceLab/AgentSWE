---
type: API reference
title: Name formatting
openwiki:
  source_paths: [src/names.mjs]
  symbols: [formatName]
---

<a id="name-formatting"></a>
# Name formatting

<!-- openwiki:generated:start id="names-api" -->
`formatName(name)` is exported from `src/names.mjs` and returns the uppercase name.

<!-- openwiki:example {"id":"names-basic","file":"names-basic.mjs","command":["node","openwiki/api/names-basic.mjs"],"expected_stdout":"ADA\n","language":"javascript","complete":true} -->
```javascript
import { formatName } from "../../src/names.mjs";
console.log(formatName("ada"));
```
<!-- openwiki:generated:end id="names-api" -->

HANDWRITTEN: Keep title casing examples out of this API page.

The clock is documented in [Clock reference](../reference/clock.md#clock-reference).
