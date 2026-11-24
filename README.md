# VIctory ConstructBVH4 test for native Windows

Run the separate-buffer D3D12 constructor test on your GPU using your own local copy of `XboxBvhBuilder.dll`. The script builds the original Rust test, generates the four synthetic scenes, translates two shaders from your DLL, and checks all 16 fixture files against the checkpoint hashes before launching the test. The only game file it reads is the builder DLL.

## Quick start

Use 64-bit Windows, a hardware D3D12 adapter with Shader Model 6.6 and wave operations, Git, 64-bit Python 3.11 or newer, and Rust with the MSVC target. Install the Visual Studio C++ x64 build tools and a Windows SDK. Open an **x64 Developer PowerShell** so Cargo can find the C compiler, headers, and linker. Leave drive letter `Z:` free for the test's temporary mapping.

```powershell
git clone https://github.com/GrapeJuice429/victory-constructbvh4-test.git
cd victory-constructbvh4-test
rustup toolchain install 1.98.1-x86_64-pc-windows-msvc
$env:RUSTUP_TOOLCHAIN = '1.98.1-x86_64-pc-windows-msvc'
py -3 run_fixture.py 'D:\path\to\your\XboxBvhBuilder.dll'
```

You can also pass the directory containing the DLL. The first run downloads the pinned public source and Microsoft DXC release and builds the native components. Generated shader files, executables, and logs stay in the local directory printed by the script. Use `--work-dir C:\path\to\a-new-directory` to choose another location. The runner grants its disposable Windows AppContainer read access to the generated fixture and test executable.

## Read the result

A successful run ends with `PASS: all four ordered constructor readbacks and guards matched` and the path to `stdout.log`, `stderr.log` and `result.json`. The wrapper also prints the SHA-256 of your rebuilt test executable. On a multi-GPU machine the test uses the default hardware adapter. Record the adapter and driver when sharing results.

Each case compares all 4,096 dwords of the contiguous reference output. The separate-buffer check puts eight shader roots on seven resource spans, writes the count on the GPU, dispatches the constructor, scatters six spans, and copies the result into a second buffer. After the fence wait, it compares the result copy, original constants, owners and guard bytes.

| Instance count | Count written before dispatch | Expected contiguous differences | Expected ordered check |
| ---: | ---: | ---: | --- |
| 2 | 0 | 0 | Pass, including the no-change control |
| 1 | 1 | 0 | Pass |
| 2 | 2 | 0 | Pass |
| 3 | 3 | 0 | Pass |

The archived native Windows run passed all four cases on an NVIDIA GeForce RTX 4070 Ti SUPER, driver 610.88, SM 6.6 and wave width 32. Its executable SHA-256 was `832e6735ffcce96fc2134d83d6a1a2c76165c1849f2e4b74fed8ef8b70d09859`. Local linker and build paths can change the rebuilt executable hash. The test exercises constructor ordering on known synthetic resources. Live title dispatch and scene rendering are separate measurements.

## Source and fixture provenance

- `constructor-test.patch` contains the five test-side source changes against [Marvisak/VIctory at ffe310e](https://github.com/Marvisak/VIctory/tree/ffe310eb2098e52169f4c44db5ffdabf43a72622).
- `scripts/generate_cases.py` builds the synthetic inputs and computes the four expected heaps with a scalar model of these shallow trees. Every generated input and output must match its original checkpoint SHA-256. The original expected heaps were produced by running the constructor on a software Vulkan implementation.
- `exporter/` builds the shader translator and C ABI root-signature DLL against [ivall/xcl at b7aa9f3](https://github.com/ivall/xcl/tree/b7aa9f3bd212088e0d927439284dc5ed325ce3c7). Shader selection uses the two content hashes from the checkpoint. A DLL from a different game build can fail that check.
- The runtime uses the x64 DLLs from [Microsoft DXC v1.8.2505.1](https://github.com/microsoft/DirectXShaderCompiler/releases/tag/v1.8.2505.1). The wrapper verifies the release archive before extracting them.

See the [repository license](LICENSE) and the [XCL exporter's license](exporter/LICENSE).
