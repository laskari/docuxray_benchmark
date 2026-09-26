import json
import os
import sys

def main():
    run_dir = "runs/sanity_check-20260907-092554"
    gt_path = "gt/receipt/sroie/ground_truth.jsonl"
    
    gt_docs = {}
    with open(gt_path) as f:
        for line in f:
            d = json.loads(line)
            gt_docs[d["doc_id"]] = d["gt"]
            
    raw_dir = os.path.join(run_dir, "raw")
    if not os.path.exists(raw_dir):
        print("Raw dir not found")
        return
        
    mismatches = []
    
    for filename in sorted(os.listdir(raw_dir)):
        if not filename.endswith(".json"):
            continue
        doc_id = filename.split(".")[0]
        if doc_id not in gt_docs:
            continue
            
        with open(os.path.join(raw_dir, filename)) as f:
            pred = json.load(f)["arms"]["FINAL"]
            
        gt = gt_docs[doc_id]
        
        # Check parties.seller.name
        gt_name = gt.get("parties.seller.name")
        pred_name = pred.get("parties", {}).get("seller", {}).get("name")
        
        # Check address
        gt_addr = gt.get("parties.seller.addressStructured")
        pred_addr = pred.get("parties", {}).get("seller", {}).get("addressStructured", {}).get("address")
        
        if gt_name != pred_name:
            mismatches.append(f"[{doc_id}] SELLER NAME MISMATCH:\n  GT  : {repr(gt_name)}\n  PRED: {repr(pred_name)}\n")
            
        if gt_addr != pred_addr:
            mismatches.append(f"[{doc_id}] ADDRESS MISMATCH:\n  GT  : {repr(gt_addr)}\n  PRED: {repr(pred_addr)}\n")
            
    print(f"Total mismatches analyzed: {len(mismatches)}")
    for m in mismatches[:10]: # just show first 10
        print(m)

if __name__ == "__main__":
    main()
