"""Build the downloadable agent deterministically, independent of checkout EOLs."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.upgrade_contract import FILES, MANIFEST, package_bytes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "agent"
    target = source / "appbox-agent-latest.zip"
    expected = package_bytes(source)
    if args.check:
        if target.read_bytes() != expected:
            raise SystemExit("Agent archive is stale: run python scripts/package_agent.py")
        print("Agent package reproducible and current")
    else:
        target.write_bytes(expected)
