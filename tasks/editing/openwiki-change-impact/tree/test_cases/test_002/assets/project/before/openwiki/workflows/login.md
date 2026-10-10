---
type: workflow
title: Login
openwiki:
  source_paths: [src/login.js]
  symbols: [login]
  depends_on: [service/profile]
---

<a id="login-workflow"></a>
# Login workflow

<!-- openwiki:generated:start id="login-id" -->
Login sessions contain the numeric user ID supplied by the [profile service](../service/profile.md#profile-service).

<!-- openwiki:example {"id":"login-flow","file":"login-flow.js","command":["node","openwiki/workflows/login-flow.js"],"expected_stdout":"session:7\n","language":"javascript","complete":true} -->
```javascript
import { login } from "../../src/login.js";
console.log(login());
```
<!-- openwiki:generated:end id="login-id" -->

HANDWRITTEN WORKFLOW NOTE
