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
A login session contains the numeric user ID provided by the [profile service](../service/profile.md#profile-service).

<!-- openwiki:example {"id":"login-flow","file":"login-flow.js","command":["node","openwiki/workflows/login-flow.js"],"expected_stdout":"session:7\n","language":"javascript","complete":true} -->
```javascript
import { login } from "../../src/login.js";
console.log(login());
```
<!-- openwiki:generated:end id="login-id" -->

Hand-written workflow note

