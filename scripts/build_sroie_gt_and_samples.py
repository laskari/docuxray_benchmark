import os
import json
import random
from registry import dataset_for

def main():
    entry = dataset_for("receipt", "sroie")
    adapter = entry.load()
    
    gt_dir = os.path.join("gt", "receipt", "sroie")
    os.makedirs(gt_dir, exist_ok=True)
    
    gt_path = os.path.join(gt_dir, "ground_truth.jsonl")
    
    docs = []
    with open(gt_path, "w") as f:
        for record in adapter.iter_records():
            f.write(record.to_json() + "\n")
            docs.append(record.doc_id)
            
    print(f"Wrote {len(docs)} records to {gt_path}")
    
    # Generate sampling plans
    # 50 sanity check
    # 3x500 batches
    random.seed(42)
    random.shuffle(docs)
    
    sanity = docs[:50]
    batch1 = docs[50:550]
    batch2 = docs[550:1050]
    batch3 = docs[1050:1550] if len(docs) > 1050 else []
    
    def write_plan(name, doc_list, purpose):
        if not doc_list:
            return
        plan = {
            "n_docs": len(doc_list),
            "n_templates": len(doc_list),
            "purpose": purpose,
            "selection": "random sample",
            "doc_ids": doc_list
        }
        with open(os.path.join(gt_dir, f"{name}.json"), "w") as f:
            json.dump(plan, f, indent=2)
            
    write_plan("sanity_check", sanity, "50-doc sanity check")
    write_plan("batch_1", batch1, "first batch of 500")
    write_plan("batch_2", batch2, "second batch of 500")
    write_plan("batch_3", batch3, "third batch of 500")
    
    print("Generated sampling plans")

if __name__ == "__main__":
    main()
