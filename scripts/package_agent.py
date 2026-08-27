"""Build the downloadable agent deterministically, independent of checkout EOLs."""
import argparse
import io
from pathlib import Path
import zipfile

FILES = ("install-agent.sh", "marinos-appbox-agent.py", "marinos-appbox-agent.service", "reference_contract.py")


def package_bytes(source: Path) -> bytes:
    output = io.BytesIO()
    # ZIP_STORED also avoids differences between zlib versions/platforms.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in FILES:
            data = source.joinpath(name).read_bytes().replace(b"\r\n", b"\n")
            if b"\r" in data:
                raise ValueError(f"Unsupported bare CR in {name}")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if name in ("install-agent.sh", "marinos-appbox-agent.py") else 0o100644) << 16
            archive.writestr(info, data)
    return output.getvalue()


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
