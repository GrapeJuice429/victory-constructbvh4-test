// SPDX-License-Identifier: GPL-2.0-only
//! Export the same compute shader configurations that XCL's D3D12 runtime uses.
use std::{env, fs, path::PathBuf};
use xcl::gpu::recomp::pso::{Stage, console_to_dxil_in};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args_os().skip(1);
    let input = args.next().ok_or("expected a DXBC path")?;
    let output = PathBuf::from(args.next().ok_or("expected an output directory")?);
    if args.next().is_some() {
        return Err("expected only a DXBC path and output directory".into());
    }
    let dxbc = fs::read(input)?;
    fs::create_dir_all(&output)?;
    for windows in [1, 4] {
        for split in [false, true] {
            let compiled = console_to_dxil_in(&dxbc, Stage::Compute, 0, split, windows)?;
            fs::write(output.join(format!("windows{windows}-split{split}.dxil")), compiled.dxil)?;
        }
    }
    Ok(())
}
