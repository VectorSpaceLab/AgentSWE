import Proof.Tree

namespace Hidden.Codec.Target

open Hidden.Codec

theorem list_roundTrip [Codec α] (xs : List α) :
    List.map roundTrip xs = xs := by
  -- Preserve the arbitrary type and inferred Codec instance.

theorem tree_roundTrip [Codec α] (tree : Tree α) :
    Tree.map roundTrip tree = tree := by
  -- A second polymorphic induction over the custom tree.

theorem bool_sample : Tree.map roundTrip Tree.sample = Tree.sample := by
  exact tree_roundTrip Tree.sample

theorem encoded_nat_pair :
    encodePair ((2 : Nat), (5 : Nat)) = ((3 : Nat), (6 : Nat)) := by
  rfl

end Hidden.Codec.Target
