// SPDX-License-Identifier: GPL-2.0-only
//! Expose XCL's compute root-signature factory to the separate VIctory test executable.
use std::ffi::c_void;
use windows::Win32::Graphics::Direct3D12::ID3D12Device;
use windows::core::Interface;

/// The caller passes a borrowed D3D12 device and receives one owned root-signature reference.
///
/// # Safety
/// The device must be a live ID3D12Device and output must point to a writable COM pointer.
#[unsafe(no_mangle)]
pub unsafe extern "system" fn xcl_compute_root(device: *mut c_void, output: *mut *mut c_void) -> i32 {
    if device.is_null() || output.is_null() {
        return 0x8000_4003u32 as i32; // E_POINTER
    }
    unsafe { output.write(std::ptr::null_mut()) };
    let device = unsafe { ID3D12Device::from_raw_borrowed(&device) }.unwrap();
    match xcl::gpu::recomp::pso::abi_root_signature(device, false) {
        Ok(signature) => {
            unsafe { output.write(signature.into_raw()) };
            0
        }
        Err(error) => {
            eprintln!("XCL compute root signature: {error}");
            0x8000_4005u32 as i32 // E_FAIL
        }
    }
}
