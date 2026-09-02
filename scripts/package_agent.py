"""Build the downloadable agent deterministically, independent of checkout EOLs."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.upgrade_contract import BRIDGE_FILES, FILES, MANIFEST, package_bytes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "agent"
    packages = {
        source / "appbox-agent-latest.zip": package_bytes(source, FILES),
        source / "appbox-agent-bridge.zip": package_bytes(source, BRIDGE_FILES),
    }
    if args.check:
        if any(target.read_bytes() != expected for target, expected in packages.items()):
            raise SystemExit("Agent archives are stale: run python scripts/package_agent.py")
        print("Agent package reproducible and current")
    else:
        for target, expected in packages.items():
            target.write_bytes(expected)
