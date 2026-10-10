---
type: service
title: Profile service
openwiki:
  source_paths: [src/profile.js]
  symbols: [profileId]
  depends_on: [model/user]
---

<a id="profile-service"></a>
# Profile service

<!-- openwiki:generated:start id="profile-id" -->
The profile service exposes the number-valued ID from the [User model](../model/user.md#user-model).

<!-- openwiki:example {"id":"profile-service","file":"profile-service.js","command":["node","openwiki/service/profile-service.js"],"expected_stdout":"7\n","language":"javascript","complete":true} -->
```javascript
import { profileId } from "../../src/profile.js";
console.log(profileId());
```
<!-- openwiki:generated:end id="profile-id" -->

HANDWRITTEN SERVICE NOTE
