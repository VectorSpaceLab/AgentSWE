namespace Hidden.Codec

class Codec (α : Type u) where
  Encoded : Type u
  encode : α → Encoded
  decode : Encoded → α
  decode_encode : ∀ x, decode (encode x) = x

def roundTrip [Codec α] (x : α) : α := Codec.decode (Codec.encode x)

namespace Laws

theorem roundTrip_eq [Codec α] (x : α) : roundTrip x = x := by
  exact Codec.decode_encode x

theorem congr_roundTrip [Codec α] (f : α → β) (x : α) :
    f (roundTrip x) = f x := by
  rw [roundTrip_eq]

end Laws

instance boolCodec : Codec Bool where
  Encoded := Bool
  encode := id
  decode := id
  decode_encode := by intro x; rfl

instance natCodec : Codec Nat where
  Encoded := Nat
  encode := fun n => n + 1
  decode := fun n => n - 1
  decode_encode := by intro n; simp

def encodePair [Codec α] [Codec β] (pair : α × β) :
    Codec.Encoded α × Codec.Encoded β :=
  (Codec.encode pair.1, Codec.encode pair.2)

namespace Legacy

def roundTrip (n : Nat) : Nat := n + 0

namespace Laws

theorem roundTrip_eq (n : Nat) : roundTrip n = n := by
  simp [roundTrip]

end Laws
end Legacy

end Hidden.Codec
