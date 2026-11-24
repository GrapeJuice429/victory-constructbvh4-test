"""Rebuild and run the native Windows ConstructBVH4 checkpoint with locally owned game files."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent
VICTORY = "ffe310eb2098e52169f4c44db5ffdabf43a72622"
XCL = "b7aa9f3bd212088e0d927439284dc5ed325ce3c7"
XCL_ARCHIVE_SHA = "d07b1e53b0a4335b92c571d4dd4790547fefe9b2db6c75c94642e62e3f4457ef"
DXC_URL = "https://github.com/microsoft/DirectXShaderCompiler/releases/download/v1.8.2505.1/dxc_2025_07_14.zip"
DXC_SHA = "9ad895a6b039e3a8f8c22a1009f866800b840a74b50db9218d13319e215ea8a4"
TARGET = "x86_64-pc-windows-msvc"
TEST = "host::xcl_header_test::public_xcl_headers_on_victory_host"


def sha256(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def checkout(repository, commit, directory):
    subprocess.run(["git", "init", "--quiet", str(directory)], check=True)
    prefix = ["git", "-C", str(directory)]
    subprocess.run([*prefix, "fetch", "--quiet", "--depth=1",
                    f"https://github.com/{repository}.git", commit], check=True)
    subprocess.run([*prefix, "checkout", "--quiet", "--detach", "FETCH_HEAD"], check=True)
    head = subprocess.check_output([*prefix, "rev-parse", "HEAD"], text=True).strip()
    if head != commit:
        raise RuntimeError(f"Unexpected revision in {directory}: {head}")


def provision_dxc(work, probe):
    package = work / "dxc.zip"
    print("Downloading Microsoft's pinned x64 DXC release.", flush=True)
    urllib.request.urlretrieve(DXC_URL, package)
    if sha256(package) != DXC_SHA:
        raise ValueError("DXC release archive SHA-256 mismatch")
    with zipfile.ZipFile(package) as archive:
        for dll in ("dxcompiler.dll", "dxil.dll"):
            (probe / dll).write_bytes(archive.read("bin/x64/" + dll))


def provision_xcl(work, xcl):
    package = work / "xcl.zip"
    urllib.request.urlretrieve(f"https://codeload.github.com/xcl-dev/xcl/zip/{XCL}", package)
    if sha256(package) != XCL_ARCHIVE_SHA:
        raise ValueError("XCL source archive SHA-256 mismatch")
    with zipfile.ZipFile(package) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            prefix, relative = member.filename.split("/", 1)
            path = (xcl / relative).resolve()
            if prefix != f"xcl-{XCL}" or not path.is_relative_to(xcl):
                raise ValueError(f"Unexpected XCL source member: {member.filename}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(member))


def build_test(victory, work):
    command = ["cargo", "test", "--locked", "--profile", "diag", "--target", TARGET,
               "-p", "victory-gpu", "--lib", "--no-run", "--message-format=json"]
    messages = work / "victory-build.jsonl"
    with messages.open("wb") as output:
        subprocess.run(command, cwd=victory, stdout=output, check=True)
    binaries = []
    for line in messages.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if (record.get("reason") == "compiler-artifact" and record["profile"]["test"]
                and record["target"]["name"] == "victory_gpu" and record["executable"]):
            binaries.append(Path(record["executable"]))
    if len(binaries) != 1:
        raise RuntimeError(f"Expected one victory-gpu test executable, got {binaries}")
    return binaries[0]


def check_readbacks(stdout, stderr, result):
    if result["exit_code"] != 0 or result["timed_out"] or result["capabilities"] != 0:
        raise RuntimeError(f"AppContainer test did not finish successfully: {result}")
    if not re.search(r"SM 0x[0-9a-f]+, wave range \d+\.\.\d+, wave ops=true", stderr):
        raise RuntimeError("Missing device shader-model/wave capability line")
    if "test result: ok. 1 passed; 0 failed; 0 ignored;" not in stdout:
        raise RuntimeError("The named Rust test did not pass")
    for windows in (1, 4):
        if not re.search(rf"^windows={windows} split=(true|false): two 64-byte headers, optional 24-byte bounds "
                         r"and every sentinel byte in all four 4096-byte allocations matched$", stdout, re.M):
            raise RuntimeError(f"Missing header readback for {windows} window(s)")
    for instances, initial in [(2, 0), (1, 1), (2, 2), (3, 3)]:
        case = f"instances-{instances}-initial-{initial}"
        reference = f"ConstructBVH4 {case}: 0 differing dwords of 4096, first actual/expected []"
        ordered = (f"{case}: eight roots on seven resource subspans, six scatter spans, earlier count write "
                   "and later result copy matched, original constants and every owner/arena guard preserved")
        if reference not in stdout.splitlines() or ordered not in stdout.splitlines():
            raise RuntimeError(f"Missing contiguous or ordered readback for {case}")


def run_test(stage, test):
    if ctypes.windll.kernel32.GetLogicalDrives() & (1 << 25):
        raise RuntimeError("Z: is already in use. The archived test requires a free Z: drive.")
    subprocess.run(["subst.exe", "Z:", str(stage)], check=True)
    try:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "appcontainer_run.py"),
                        "--stage", str(stage), "--read", "fixture", "--read", "probe", "--write", "state",
                        "--cwd", "probe", "--timeout", "180", "--env", "XCL_VICTORY_BRIDGE=1",
                        "--env", f"VICTORY_DXIL={stage / 'probe' / 'dxil.dll'}",
                        "--", str(test), TEST, "--exact", "--nocapture"])
    finally:
        if Path("Z:/").exists() and os.path.samefile("Z:/", stage):
            subprocess.run(["subst.exe", "Z:", "/D"], check=True)
    logs = list((stage / "logs").glob("*/result.json"))
    if len(logs) != 1:
        raise RuntimeError(f"Expected one runner result in {stage / 'logs'}, found {len(logs)}")
    log = logs[0].parent
    stdout = (log / "stdout.log").read_text(encoding="utf-8")
    stderr = (log / "stderr.log").read_text(encoding="utf-8")
    print(stdout, end="")
    print(stderr, end="", file=sys.stderr)
    check_readbacks(stdout, stderr, json.loads(logs[0].read_bytes()))
    return log


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game", type=Path, help="Local game directory or XboxBvhBuilder.dll itself")
    parser.add_argument("--work-dir", type=Path, help="Fresh local NTFS directory for generated files and logs")
    args = parser.parse_args()
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8 or sys.version_info < (3, 11):
        parser.error("Use native Windows x64 with 64-bit Python 3.11 or newer.")
    builder = (args.game / "XboxBvhBuilder.dll" if args.game.is_dir() else args.game).resolve()
    if not builder.is_file():
        parser.error("XboxBvhBuilder.dll was not found at the supplied path.")
    if ctypes.windll.kernel32.GetLogicalDrives() & (1 << 25):
        parser.error("The archived test uses Z:. Run it on a Windows session with Z: free.")
    for command in ("git", "cargo"):
        if not shutil.which(command):
            parser.error(f"{command} must be on PATH.")
    if args.work_dir:
        work = args.work_dir.resolve()
        work.mkdir(parents=True, exist_ok=False)
    else:
        work = Path(tempfile.mkdtemp(prefix="victory-constructor-", dir=os.environ["LOCALAPPDATA"]))
    print(f"Local build and evidence directory: {work}", flush=True)
    victory, xcl, stage = work / "victory", work / "xcl", work / "stage"
    fixture, probe = stage / "fixture", stage / "probe"
    for folder in (fixture / "constructor" / "cases", probe, stage / "state"):
        folder.mkdir(parents=True)
    (stage / ".victory-appcontainer-stage").touch()
    provision_dxc(work, probe)
    checkout("Marvisak/VIctory", VICTORY, victory)
    provision_xcl(work, xcl)
    subprocess.run(["git", "-C", str(victory), "apply", "--check", str(ROOT / "constructor-test.patch")], check=True)
    subprocess.run(["git", "-C", str(victory), "apply", str(ROOT / "constructor-test.patch")], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_cases.py"),
                    str(fixture / "constructor" / "cases")], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "export_shaders.py"), "--xcl", str(xcl),
                    "--builder", str(builder), "--fixture", str(fixture), "--probe", str(probe),
                    "--work", str(work)], check=True)
    manifest = (ROOT / "fixture.sha256").read_text(encoding="ascii").splitlines()
    for line in manifest:
        digest, name = line.split("  ", 1)
        if sha256(fixture / name) != digest:
            raise ValueError(f"Fixture SHA-256 mismatch: {name}")
    print(f"Verified {len(manifest)} checkpoint fixture files.", flush=True)
    test = probe / "constructor-test.exe"
    shutil.copyfile(build_test(victory, work), test)
    print("Rebuilt test SHA-256:", sha256(test), flush=True)
    log = run_test(stage, test)
    print(f"PASS: all four ordered constructor readbacks and guards matched. Logs: {log}")


if __name__ == "__main__":
    main()
