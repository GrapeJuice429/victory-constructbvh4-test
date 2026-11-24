"""Build the pinned XCL exporter and generate private GPU inputs on native Windows."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


# SHA-256 of scripts/re/bvhb_shaders.py at XCL b7aa9f3bd212088e0d927439284dc5ed325ce3c7.
EXTRACTOR_SHA = "e973f7ddc2633d109fa4a68c7af95520cc55ea52ffb45258ee078a76027e62fd"
INPUTS = {
    "InitHeaders": ("7bdd8b5bf476e7f20951a92c556d6f394d4ef9bd4ca3396969298d3ee1d8dd7d", ""),
    "ConstructBVH4": ("3c126475693cf47f8389b7f1a1ae0fdd99a3e72371a4ee92385968ee399b20e3", "constructor"),
}
GOLDEN = {
    "windows1-splitfalse.dxil": "1f878f818a01ae3fd38b28ce725fe3969683600211ca0b904e8f8183f097be0d",
    "windows1-splittrue.dxil": "0a5b3b2437d918c98e6ed662fe88a5aa311b54f77834c6cc0f48b22289825b18",
    "windows4-splitfalse.dxil": "31e5f4d25a9c461597bfcb22ee3c182533182b01a3cc0b31b30a40d4a023053c",
    "windows4-splittrue.dxil": "3a3e6f15fe4e90b95ac434824c77622216f4809d5f8a8cee7425322da55165c3",
    "constructor/windows1-splitfalse.dxil": "31db32932c9ee1f4d08cfeef954c4a936f12fdb82f0630e13b38d6f4696e4f97",
    "constructor/windows1-splittrue.dxil": "31db32932c9ee1f4d08cfeef954c4a936f12fdb82f0630e13b38d6f4696e4f97",
    "constructor/windows4-splitfalse.dxil": "b29b81578f5e68338690fccb631c69897f026e09e10031beade2f216cb164b0c",
    "constructor/windows4-splittrue.dxil": "b29b81578f5e68338690fccb631c69897f026e09e10031beade2f216cb164b0c",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(xcl, builder, fixture, probe, work):
    xcl, builder, fixture, probe, work = (p.resolve() for p in (xcl, builder, fixture, probe, work))
    extractor = xcl / "scripts" / "re" / "bvhb_shaders.py"
    # A Windows Git checkout may have converted LF line endings to CRLF.
    normalized = extractor.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(normalized).hexdigest() != EXTRACTOR_SHA:
        raise ValueError("The XCL builder-shader extractor differs from the pinned source.")
    for dll in ["dxcompiler.dll", "dxil.dll"]:
        if not (probe / dll).is_file():
            raise FileNotFoundError(f"Missing {probe / dll}. Supply the x64 DXC and validator together.")
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="xcl-export-", dir=work) as temp:
        temp = Path(temp)
        extracted = temp / "dxbc"
        with (temp / "extract.log").open("wb") as log:
            # The upstream extractor prints Unicode root-signature descriptions.
            subprocess.run([sys.executable, str(extractor), str(builder), str(extracted)], check=True,
                           stdout=log, env=os.environ | {"PYTHONIOENCODING": "utf-8"})
        containers = {sha(path): path for path in extracted.glob("*.dxbc")}
        for name, (digest, _) in INPUTS.items():
            if digest not in containers:
                raise ValueError(f"{name} was not found in this local builder DLL (expected SHA-256 {digest}).")

        source = Path(__file__).resolve().parents[1] / "exporter"
        crate = temp / "exporter"
        crate.mkdir()
        for name in ["Cargo.toml", "Cargo.lock"]:
            shutil.copyfile(source / name, crate / name)
        shutil.copytree(source / "src", crate / "src")
        # Use the caller's pinned XCL source checkout. Path dependencies have no
        # filesystem location in Cargo.lock, so this keeps the lockfile unchanged.
        manifest = crate / "Cargo.toml"
        manifest.write_text(manifest.read_text().replace('"../../xcl/crates/xcl"',
                            json.dumps((xcl / "crates" / "xcl").as_posix(), ensure_ascii=False)), encoding="utf-8")
        target = work / "xcl-export-target"
        triple = "x86_64-pc-windows-msvc"
        # libudis86-sys 0.2.1 forces string.h with GCC syntax. MSVC and
        # clang-cl use /FI instead; otherwise memset has no C declaration.
        build_env = os.environ.copy()
        cflags = "CFLAGS_x86_64_pc_windows_msvc"
        build_env[cflags] = (build_env.get(cflags, "") + " /FIstring.h").strip()
        subprocess.run(["cargo", "build", "--locked", "--release", "--target", triple,
                        "--target-dir", str(target)], check=True, cwd=crate, env=build_env)
        binary_dir = target / triple / "release"
        generated = temp / "fixture"
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("XCL_")}
        environment.update(XCL_DXC_DIR=str(probe), XCL_DXIL_CACHE=str(temp / "dxil-cache"))
        for name, (digest, subdir) in INPUTS.items():
            print(f"Exporting {name}, source SHA-256 {digest}", flush=True)
            subprocess.run([str(binary_dir / "xcl-header-export.exe"), str(containers[digest]),
                            str(generated / subdir)], check=True, cwd=temp, env=environment)
        for name, expected in GOLDEN.items():
            actual = sha(generated / name)
            if actual != expected:
                raise ValueError(f"{name}: got {actual}, expected {expected}. Check the pinned XCL and DXC versions.")
        for name, digest in GOLDEN.items():
            destination = fixture / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(generated / name, destination)
            print(f"{digest}  {name}")
        root_dll = probe / "bridge_root.dll"
        shutil.copyfile(binary_dir / root_dll.name, root_dll)
        print(f"{sha(root_dll)}  {root_dll.name} (locally built)")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    for option in ["xcl", "builder", "fixture", "probe", "work"]:
        cli.add_argument("--" + option, type=Path, required=True)
    export(**vars(cli.parse_args()))
