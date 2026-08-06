"""Run the full pipeline in order. Exit non-zero if any stage fails.

Usage:
  python run_all.py            # live network fetch
  APC_FIXTURES=1 python run_all.py   # build from bundled sample data
"""
import subprocess
import sys
from pathlib import Path

STAGES = ["fetch_jct.py", "fetch_metadata.py", "merge.py",
          "validate.py", "build_site.py"]
HERE = Path(__file__).resolve().parent


def main() -> None:
    for stage in STAGES:
        print(f"\n=== {stage} ===")
        r = subprocess.run([sys.executable, str(HERE / stage)])
        if r.returncode != 0:
            print(f"\nPipeline halted at {stage} (exit {r.returncode}). "
                  "Last good site is unchanged.", file=sys.stderr)
            sys.exit(r.returncode)
    print("\nPipeline complete — _site/ is ready to deploy.")


if __name__ == "__main__":
    main()
