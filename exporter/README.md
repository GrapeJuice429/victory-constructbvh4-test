# XCL compute exporter

This GPL-2.0-only executable calls the compute shader compiler from [ivall/xcl at b7aa9f3](https://github.com/ivall/xcl/tree/b7aa9f3bd212088e0d927439284dc5ed325ce3c7). The companion DLL calls that project's compute root-signature factory on the device supplied by the fixture. XCL stays in these two components. The VIctory test loads the DLL through its C ABI. See the [exporter license](LICENSE).

The Python export script stages this crate and its lockfile in the user's build directory. It invokes the [XCL builder-shader extractor](https://github.com/ivall/xcl/blob/b7aa9f3bd212088e0d927439284dc5ed325ce3c7/scripts/re/bvhb_shaders.py) on the tester's own DLL, identifies the two inputs by content digest, and verifies the eight resulting DXIL files against the reference hashes.
