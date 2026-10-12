import Proof.Facts

namespace Training.Warehouse
namespace Shipping

open Batch

def manifestA : Batch := Batch.put 4 (Batch.singleton 6)

def manifestB : Batch := Batch.put 1 (Batch.singleton 2)

theorem manifestA_count : Batch.count manifestA = 2 := by
  rfl

theorem shipped_mass (left right : Batch) :
    Batch.mass (Batch.merge left right) = Batch.mass left + Batch.mass right := by
  -- Complete this proof with the repository lemma required by the request.

theorem empty_manifest_mass : Batch.mass (Batch.merge Batch.empty manifestB) = 3 := by
  rfl

end Shipping
end Training.Warehouse
