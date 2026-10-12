---
type: data model
title: User
openwiki:
  source_paths: [src/model.js]
  symbols: [makeUser]
---

<a id="user-model"></a>
# User model

<!-- openwiki:generated:start id="user-id-type" -->
A user has a numeric ID, for example `id: 7`.

<!-- openwiki:example {"id":"user-model","file":"user-model.js","command":["node","openwiki/model/user-model.js"],"expected_stdout":"number:7\n","language":"javascript","complete":true} -->
```javascript
import { makeUser } from "../../src/model.js";
const user = makeUser();
console.log(`${typeof user.id}:${user.id}`);
```
<!-- openwiki:generated:end id="user-id-type" -->

Hand-written model note

