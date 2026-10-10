# Namespaces, typeclasses, and same-named premises

Repair the pinned project in `assets/repository` after inspecting all modules. Two namespaces each define a class named `Action`, a function named `apply`, and several theorems named `unit_act`; the target file opens both namespaces.

Complete `Hidden.Ambiguous.Target.additive_identity` and `Hidden.Ambiguous.Target.prefix_identity` in `Main.lean` only. Preserve imports and declaration statements exactly. The first proof must reference `Hidden.Ambiguous.Additive.unit_act`; the second must reference `Hidden.Ambiguous.Prefix.unit_act`. Each proof body is limited to five noncomment nonblank lines. Qualification must resolve the intended class/instance without duplicating a theorem or adding an instance.

Run a clean `lake build` and list both targets and the one changed file in the reports. Do not use `sorry`, `admit`, `unsafe`, `axiom`, `opaque`, `aesop`, `native_decide`, or tactic-suggestion placeholders.
