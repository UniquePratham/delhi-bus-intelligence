import json

d = json.load(open("data/artifacts/eval_traffic.json", encoding="utf-8"))
print("holdout:", d["holdout_day"], "runs:", d["n_eval_runs"])
for k, v in d["baselines"].items():
    print(
        f"{k:16s} mae={v['mae_s']:6.2f} rmse={v['rmse_s']:6.2f} "
        f"med_ae={v['median_ae_s']:6.2f} bias={v['bias_s']:6.2f}"
    )
print("\nlevel usage:")
for k, v in d["model_level_usage"].items():
    print(f"  {k:12s} {v['n']:>9,} {v['pct']:5.1f}%")
