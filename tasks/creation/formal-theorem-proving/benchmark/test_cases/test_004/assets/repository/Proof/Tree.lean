import Proof.Codec

namespace Hidden.Codec

inductive Tree (α : Type u) where
  | leaf (value : α)
  | branch (left right : Tree α)
deriving Repr

namespace Tree

def map (f : α → β) : Tree α → Tree β
  | leaf value => leaf (f value)
  | branch left right => branch (map f left) (map f right)

def size : Tree α → Nat
  | leaf _ => 1
  | branch left right => size left + size right

def leaves : Tree α → List α
  | leaf value => [value]
  | branch left right => leaves left ++ leaves right

def sample : Tree Bool := branch (leaf true) (branch (leaf false) (leaf true))

theorem map_size (f : α → β) (tree : Tree α) : size (map f tree) = size tree := by
  induction tree with
  | leaf value => rfl
  | branch left right ihLeft ihRight => simp [map, size, ihLeft, ihRight]

theorem leaves_map (f : α → β) (tree : Tree α) :
    leaves (map f tree) = List.map f (leaves tree) := by
  induction tree with
  | leaf value => rfl
  | branch left right ihLeft ihRight => simp [map, leaves, ihLeft, ihRight]

theorem sample_size : size sample = 3 := by
  rfl

end Tree
end Hidden.Codec
