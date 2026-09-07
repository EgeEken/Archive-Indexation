"""Render recorded aggregate measurements without accessing photographs."""
from pathlib import Path

import photo_select as ps
from rebuild_review import experiment_data
from review_ui import write_page

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    ps.ROOT = root / "evidence"
    data = experiment_data()
    data.update(home="README.md", report="REPORT-V2.md", preview="README.md",
                csv="evidence/results/per_session_metrics.csv")
    write_page(root / "experiments.html", data)
    print("Created experiments.html")
