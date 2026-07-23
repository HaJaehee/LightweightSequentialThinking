"""Unpack the bundled Python runtime on the destination machine.

    python tools/setup_runtime.py          (any Python)
    ...or double-click nothing - see the manual if no Python exists at all.

Extracts runtime/python-3.x.y-embed-amd64.zip into runtime/ and makes the project
importable from it.

Why the archive ships the python.org zip verbatim instead of a pre-extracted tree:
a security reviewer can hash it against the SHA-256 published on python.org and confirm
the interpreter was not modified in transit. Pre-extracting would destroy that proof.

The one local change made here is appending a line to python3xx._pth. The embeddable
distribution runs in isolated mode, where the script directory is NOT added to sys.path
and PYTHONPATH is ignored; without this line `import planning` fails.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def find_archive() -> Path | None:
    if not RUNTIME.is_dir():
        return None
    candidates = sorted(RUNTIME.glob("python-*embed*.zip"))
    return candidates[0] if candidates else None


def patch_pth(runtime_dir: Path) -> bool:
    """Add the project root to the interpreter's fixed sys.path."""
    pth_files = list(runtime_dir.glob("python*._pth"))
    if not pth_files:
        print("  ! no python3xx._pth found - this may not be an embeddable distribution")
        return False
    pth = pth_files[0]
    # utf-8-sig: a BOM in this file breaks CPython's _pth parser outright (the first
    # line stops matching python3xx.zip and the whole path config collapses). Editors
    # like Notepad and PowerShell's Set-Content introduce BOMs, so strip on read and
    # always write plain UTF-8 - re-running this script repairs a BOM-damaged file.
    raw = pth.read_text(encoding="utf-8-sig")
    lines = raw.splitlines()
    had_bom = pth.read_bytes()[:3] == b"\xef\xbb\xbf"
    if any(line.strip() == ".." for line in lines) and not had_bom:
        print(f"  = {pth.name} already points at the project root")
        return True
    if not any(line.strip() == ".." for line in lines):
        # Insert before any commented block so it stays readable.
        insert_at = len(lines)
        for i, line in enumerate(lines):
            if line.strip().startswith("#"):
                insert_at = i
                break
        lines.insert(insert_at, "..")
    with open(pth, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    if had_bom:
        print(f"  + {pth.name}: removed a BOM that was breaking the interpreter")
    print(f"  + {pth.name}: added '..' so `import planning` resolves")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even if the interpreter is already unpacked",
    )
    args = parser.parse_args()

    print("\nplanning-mcp bundled runtime setup\n")

    exe = RUNTIME / "python.exe"
    archive = find_archive()
    running_from_runtime = exe.exists() and Path(sys.executable).resolve() == exe.resolve()

    if archive is not None:
        print(f"  archive : {archive.name}")
        print(f"  sha256  : {sha256(archive)}")
        print("            ^ compare against the hash published on python.org/downloads")

    if exe.exists() and not args.force:
        # Do NOT re-extract by default. On Windows an interpreter cannot overwrite its own
        # running python.exe, and this script is expected to be run BY the bundled
        # interpreter on machines that had no Python to begin with.
        print("  = interpreter already extracted; leaving it as is (use --force to redo)")
    else:
        if archive is None:
            print("  ! No runtime/python-*-embed-*.zip found.")
            print("    This package was built without a bundled interpreter.")
            print("    Use the system Python, or see deployment manual section 3.")
            return 1
        if running_from_runtime:
            print("  ! Cannot re-extract while running from the bundled interpreter itself.")
            print("    Run this with a different Python, or delete runtime/ and start over:")
            print("      Expand-Archive .\\runtime\\" + archive.name + " -DestinationPath .\\runtime")
            return 1
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            zf.extractall(RUNTIME)
        print(f"  + extracted {len(names)} files into runtime/")

    if not exe.exists():
        print("\n  ! python.exe not found after extraction - is this really an embed zip?")
        return 1

    if not patch_pth(RUNTIME):
        return 1

    # Prove the interpreter runs and can see the project.
    print("\n  checking the extracted interpreter...")
    probe = "import sys, planning; print(sys.version.split()[0], planning.__version__)"
    try:
        proc = subprocess.run(
            [str(exe), "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=60,
        )
    except OSError as exc:
        print(f"  ! could not launch {exe}: {type(exc).__name__}: {exc}")
        return 1

    if proc.returncode != 0:
        print(f"  ! interpreter check failed:\n{(proc.stdout + proc.stderr).strip()}")
        return 1

    version, pkg_version = (proc.stdout.strip().split() + ["?"])[:2]
    print(f"  [OK  ] Python {version} runs and imports planning {pkg_version}")

    print("\n" + "=" * 68)
    print("Runtime ready. Use this interpreter everywhere from now on:")
    print(f"    {exe}")
    print("\nNext:")
    print(f"    {exe} tools\\verify_install.py")
    print("\nThen point AnythingLLM at it (deployment manual section 6):")
    print('    "command": "' + str(exe).replace("\\", "/") + '"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
