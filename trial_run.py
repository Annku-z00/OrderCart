"""Live proof: run the full pipeline for internal id 99999 -> 'Jeetu Kumawat'."""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from frameflow_config import load_config  # noqa: E402
from core.orchestrator import Orchestrator  # noqa: E402

cfg = load_config("config.yaml")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


orch = Orchestrator.from_config(
    cfg,
    output_path=os.path.join("output", "orders_trial.txt"),
    output_format="notepad",
    state_path=os.path.join("state", "trial_state.db"),
    assets_dir="assets",
    logs_dir="logs",
    review_dir="review",
    dry_run=False,
    log_callback=log,
)

log("=== RUN 99999 (Jeetu Kumawat) ===")
summary = orch.run(99999, 99999, reprocess=True, dry_run=False)
orch.close()

log("=== SUMMARY ===")
for result in summary.results:
    data = result.to_dict()
    log(
        f"id={data.get('internal_id')} status={data.get('status')} "
        f"order_id={data.get('order_id')!r} name={data.get('name')!r} "
        f"address={data.get('address')!r} images={data.get('images')} "
        f"source={data.get('source')}"
    )
log(summary.report())
