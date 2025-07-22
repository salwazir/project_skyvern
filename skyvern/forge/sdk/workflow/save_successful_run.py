import json
from pathlib import Path

def save_successful_run(run_data, output_dir="successful_runs"):
    Path(output_dir).mkdir(exist_ok=True)
    run = run_data.get("run")
        if hasattr(run, "workflow_run_id"):
            run_id = run.workflow_run_id
        elif isinstance(run, str):
            run_id = run
        else:
            run_id = "test"
    file_path = Path(output_dir) / f"{run_id}.json"
    # Convert Pydantic models to dicts for JSON serialization
    def to_dict(obj):
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        elif isinstance(obj, list):
            return [to_dict(i) for i in obj]
        elif isinstance(obj, dict):
            return {k: to_dict(v) for k, v in obj.items()}
        else:
            return obj
    with open(file_path, "w") as f:
        json.dump(to_dict(run_data), f, indent=2, default=str)
