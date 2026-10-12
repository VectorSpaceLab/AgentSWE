---
type: API reference
title: Labels
openwiki:
  source_paths: [src/labels.cjs, test/labels.test.cjs]
  symbols: [label]
---

<a id="label-defaults"></a>
# Label defaults

<!-- openwiki:generated:start id="label-default" -->
`label(name, options = {})` defaults to a colon and returns `Ada: Admin`.

<!-- openwiki:example {"id":"label-default","file":"label-default.cjs","command":["node","openwiki/api/label-default.cjs"],"expected_stdout":"Ada: Admin\n","language":"javascript","complete":true} -->
```javascript
const { label } = require("../../src/labels.cjs");
console.log(label("Ada"));
```
<!-- openwiki:generated:end id="label-default" -->

Hand-written: explicit separators remain supported for compatibility.

<!-- openwiki:example {"id":"label-explicit","file":"label-explicit.cjs","command":["node","openwiki/api/label-explicit.cjs"],"expected_stdout":"Ada / Admin\n","language":"javascript","complete":true} -->
```javascript
const { label } = require("../../src/labels.cjs");
console.log(label("Ada", { separator: " /" }));
```

