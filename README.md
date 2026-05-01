# Hunyuan3D-2mv Modly Extension

Multi-view image-to-mesh generation for Modly using Tencent Hunyuan3D-2mv.

## Supported Platforms

- **Windows**: existing install path remains supported.
- **Linux ARM64 + NVIDIA CUDA**: first supported Linux target.
- **Other Linux/macOS targets**: not claimed as supported yet.

## Windows Modly Installation Guide

This guide provides a clean installation process for Windows users using PowerShell.

### 1. Core Installation

Open a standard PowerShell window. Administrator access is not required for this part.

#### Step A: Clone Modly

```powershell
git clone https://github.com/lightningpixel/modly.git
cd "$HOME\modly\api"
```

#### Step B: Set up the Python environment

```powershell
python -m venv .venv
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Frontend Setup and Launch

Ensure Node.js is installed, then run:

```powershell
cd "$HOME\modly"
npm install
cmd /c launch.bat
```

### Windows Troubleshooting

- If Python is missing, install it and restart PowerShell:

  ```powershell
  winget install Python.Python.3.11 --override "/quiet InstallAllUsers=1 PrependPath=1"
  ```

- If `npm install` fails because Node.js is missing, install it and restart PowerShell:

  ```powershell
  winget install OpenJS.NodeJS
  ```

- If Modly cannot find its bundled Python files, run:

  ```powershell
  cd "$HOME\modly"
  node scripts/download-python-embed.js
  ```

- Do **not** install Modly inside a OneDrive folder; it can cause permission errors.
- Ensure `C:\Program Files\Git\cmd` is in your System Path.
- If model components are present but reported as missing, install the [VC Redistributable](https://aka.ms).

## Extension Installation Notes

1. Install the extension in Modly.
2. Open the Modly extensions panel and click the download button for the Hunyuan3D weights.
3. Stay on the tab until the download finishes, then restart Modly.

Hardware and performance notes:

- **VRAM**: 6 GB minimum, 8 GB or more recommended.
- **Efficiency**: the Turbo model is more memory-efficient than Standard.
- **Multi-image inputs**: until Modly core maps model inputs by `targetHandle`, connected workflow images can still collapse to one primary front image. The extension accepts optional side views when Modly passes `left_image_path`, `back_image_path`, and `right_image_path` params.
- **Optional texgen**: shape generation remains the default. Texture generation is opt-in via `include_texture=true` and is capability-gated at runtime.

## Linux ARM64 Prerequisites

You need ALL of the following:

- Linux on `aarch64`/`arm64`
- NVIDIA GPU with **SM >= 70**
- CUDA userspace/driver compatible with **CUDA 12.4** or **CUDA 12.8** wheels
- Network access to:
  - Hugging Face model downloads
  - GitHub zip download for lazy `_hy3dgen` source extraction
- A Modly install that can create the extension venv

## Install Behavior

### Linux ARM64 + NVIDIA CUDA

The installer uses an explicit Linux ARM64 branch:

- installs **pinned** ARM64 CUDA wheels for Torch/Torchvision
- does **not** require `xformers`
- installs `rembg` + CPU `onnxruntime`
- does **not** require `onnxruntime-gpu`
- does **not** clone/install `Hunyuan3D-2` editable during setup
- lazily downloads/extracts `_hy3dgen` source on first runtime import if needed

Pinned Torch targets:

- **cu124**: `torch==2.5.1`, `torchvision==0.20.1`
- **cu128 / Blackwell-tier path**: `torch==2.7.0+cu128`, `torchvision==0.22.0`

If a pinned ARM64 wheel is unavailable for the embedded Python tag, setup fails clearly instead of silently downgrading.

### Windows / non-ARM64

The prior behavior is intentionally preserved as much as possible:

- existing GPU-SM-based Torch selection remains
- `xformers` stays in the install path
- editable `Hunyuan3D-2` repo install remains the compatibility path
- the custom rasterizer build runs before the editable `hy3dgen` install
- `onnxruntime-gpu` is still attempted where it was already used, with CPU `onnxruntime` retained as fallback

## Runtime Behavior

`generator.py` resolves `hy3dgen` in this order:

1. already-installed `hy3dgen`
2. extension-local `Hunyuan3D-2/`
3. model cache `_hy3dgen/`

On Linux ARM64, if `_hy3dgen` is missing, the generator downloads the upstream GitHub zip and extracts only the `hy3dgen/` tree into the model cache.

Background removal behavior:

- default path still uses upstream `BackgroundRemover`
- on Linux ARM64 failure, runtime retries with `rembg.new_session(..., providers=["CPUExecutionProvider"])`
- if both fail, generation continues with the original image and logs the reason

Before export, generated meshes are validated for non-empty vertices and faces so failed generations stop with a clear error instead of exporting an invalid GLB.

## Optional Texture Generation (First Cut)

This repository now exposes an OPTIONAL texture path after shape generation:

- `include_texture=false` by default, so existing shape-only requests remain unchanged.
- `texture_model_variant`: `turbo` or `standard`
- `texture_input_mode`: `front` or `multiview`

### Capability Gate

When `include_texture=true`, the generator probes texgen readiness BEFORE texturing:

- CUDA availability
- `hy3dgen.texgen.Hunyuan3DPaintPipeline`
- `xatlas`
- `custom_rasterizer`
- `mesh_processor`
- local paint/delight weight folders

If any check fails, the request raises an actionable error. It does NOT silently return an untextured mesh when texture was explicitly requested.

### Required Extra Dependencies

Texgen is NOT added to `setup.py` in this first cut. That is intentional to avoid regressing the working shape path.

For a supported CUDA host, operators must provide the optional texgen runtime separately, including at minimum:

- `xatlas`
- `custom_rasterizer`
- `mesh_processor`
- a `hy3dgen` installation/source tree that includes `hy3dgen.texgen`

### Required Local Weights

Texgen uses local assets from `tencent/Hunyuan3D-2`, not the existing `tencent/Hunyuan3D-2mv` shape repo.

- The extension manages those texture assets under its own model directory, inside an extension-owned `Hunyuan3D-2/` root.
- On the first textured request, the generator lazily attempts a FILTERED Hugging Face download into that owned root.
- The filtered download targets only the selected texture variant assets, the shared delight assets, and minimal config files needed by the upstream texgen loader. The turbo paint variant also requires the standard paint `text_encoder/` and `vae/` component files when those components are omitted from the turbo folder.

If that download cannot complete because of missing network access, authentication/token issues, or incomplete upstream files, the request fails clearly and points to the extension-owned target path.

## Linux ARM64 Runtime Prep (Opt-in)

This is the SAFE boundary for experimental texgen runtime prep on Linux ARM64. `setup.py` remains shape-only by design. If you want optional textured output on Linux ARM64, do it explicitly with the runtime-prep script and DRY-RUN FIRST.

### Defaults and targeting

- Default target venv: `./venv`
- Override the target explicitly with `--venv /absolute/path/to/venv` if Modly installed the extension elsewhere.
- The script refuses non-venv/global Python targets.
- The script never clones/downloads Hunyuan3D source; pass `--source-root /path/to/Hunyuan3D-2` when mesh/custom texgen sources are needed.
- Prefer an explicit CUDA home that matches the target Torch runtime, for example `CUDA_HOME=/usr/local/cuda-12.8`, instead of relying on a generic `PATH`/`/usr/local/cuda` symlink that may point at a different CUDA version.

### What the script checks before mutation

- Linux `aarch64` / `arm64`
- target venv exists and contains `pyvenv.cfg`, `bin/python`, `bin/pip`
- target Python ABI is `cp312`
- target venv can import Torch and reports a supported CUDA suffix (`cu124` or `cu128`)
- `CUDA_HOME` / `nvcc`
- GPU SM / compute capability
- Hunyuan3D texgen source-root availability for source-build stages
- optional artifact compatibility for wheel inputs (reject x86_64, Windows, wrong ABI, or obvious runtime-tag mismatches)

### Dry-run first

Inspect only:

```bash
python3 scripts/prepare_linux_arm64_texgen_runtime.py --dry-run --stage inspect --venv venv
```

Plan the full ordered prep without mutating:

```bash
python3 scripts/prepare_linux_arm64_texgen_runtime.py \
  --dry-run \
  --stage all \
  --venv venv \
  --source-root /path/to/Hunyuan3D-2
```

If your GPU arch is not auto-detected cleanly, pin it explicitly:

```bash
CUDA_HOME=/usr/local/cuda-12.8 \
python3 scripts/prepare_linux_arm64_texgen_runtime.py \
  --dry-run \
  --stage custom_rasterizer \
  --venv venv \
  --source-root /path/to/Hunyuan3D-2 \
  --arch-list '12.0+PTX'
```

For GB10 / SM 12.1 with Torch `2.7.0+cu128`, use `12.0+PTX`; Torch rejects `12.1`.

### Ordered stages

The script is intentionally ordered and fail-fast:

1. `xatlas`
2. `mesh_processor`
3. `custom_rasterizer`
4. `probe`

### Runtime linker requirement for `custom_rasterizer`

This is the part people get wrong. `custom_rasterizer` is a compiled Torch/CUDA extension, so a successful `pip install` does **NOT** guarantee the runtime linker can resolve Torch shared libraries later.

The runtime-prep script patches the installed `custom_rasterizer_kernel*.so` RUNPATH with deterministic entries from the target venv and selected CUDA installation: the target venv Torch `lib/` directory plus the selected CUDA `lib64` directory.

Use that as the operator model: the normal UX should be venv-scoped RUNPATH patching, not requiring operators to export `LD_LIBRARY_PATH` before launching Modly.

During `--stage custom_rasterizer` and `--stage all`, the script:

1. installs/builds `custom_rasterizer`
2. locates `custom_rasterizer_kernel*.so` under the target venv site-packages
3. prefers `<venv>/bin/patchelf`, with `PATH` fallback
4. applies `patchelf --set-rpath` using the target venv Torch `lib/` directory plus CUDA `lib64`
5. verifies the resulting RUNPATH before running the import probe

For CUDA toolchain selection, the script uses `<CUDA_HOME>/bin/nvcc` or `<CUDA_PATH>/bin/nvcc` when that executable exists. If `PATH` exposes a different `nvcc` (for example `/usr/local/cuda` pointing to CUDA 13.0 while `CUDA_HOME=/usr/local/cuda-12.8`), dry-run/reporting warns that the `PATH` `nvcc` is ignored and planned CUDA commands carry the selected `CUDA_HOME`/`CUDA_PATH` environment.

Dry-run remains non-mutating: it reports the intended RUNPATH patch but does not invoke `patchelf`.

If RUNPATH patching cannot be completed and you need a diagnostic fallback only, reproduce the equivalent linker scope manually with your actual target paths:

```bash
export LD_LIBRARY_PATH="/path/to/venv/lib/python3.12/site-packages/torch/lib:/usr/local/cuda-12.8/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
venv/bin/python -c "import custom_rasterizer"
```

Then run the failing import/probe command from the same shell. Do not treat that export as the normal Modly launch path; fix or re-run the RUNPATH patch instead.

The prep script does **not** claim any global install or global linker configuration. The RUNPATH patch is scoped to the installed extension shared object inside the target venv.

Per-stage examples (THESE MUTATE when `--dry-run` is omitted):

```bash
python3 scripts/prepare_linux_arm64_texgen_runtime.py --stage xatlas --venv venv
python3 scripts/prepare_linux_arm64_texgen_runtime.py --stage mesh_processor --venv venv --source-root /path/to/Hunyuan3D-2
CUDA_HOME=/usr/local/cuda-12.8 python3 scripts/prepare_linux_arm64_texgen_runtime.py --stage custom_rasterizer --venv venv --source-root /path/to/Hunyuan3D-2 --arch-list '12.0+PTX'
python3 scripts/prepare_linux_arm64_texgen_runtime.py --stage probe --venv venv --source-root /path/to/Hunyuan3D-2
```

### Optional artifact inputs

If you already have local artifacts, the script can validate and prefer them:

- `--xatlas-wheel /path/to/xatlas-...whl`
- `--mesh-processor-wheel /path/to/mesh_processor-...whl`
- `--custom-rasterizer-wheel /path/to/custom_rasterizer-...whl`

If no compatible artifact is supplied:

- `xatlas` falls back to a source/sdist install plan.
- `mesh_processor` builds from `hy3dgen/texgen/differentiable_renderer`.
- `custom_rasterizer` builds from `hy3dgen/texgen/custom_rasterizer` with `TORCH_CUDA_ARCH_LIST` from `--arch-list` or detected SM; on GB10/SM 12.1, use `12.0+PTX` for Torch `2.7.0+cu128`.

### Cleanup and rollback

- `--clean` only removes script-owned temp/build dirs under `.texgen-runtime-prep/`.
- It does **not** delete your venv or upstream source checkout.

Targeted rollback:

```bash
venv/bin/pip uninstall xatlas mesh_processor custom_rasterizer
```

Full rollback:

```bash
rm -rf venv
```

Then let Modly recreate the extension venv and re-run the script in `--dry-run` mode before trying again.

### Final smoke guidance

1. Dry-run the full plan first.
2. Run the required mutating stages one by one, stopping after the first failure.
3. Confirm the final probe imports:

```bash
env -u LD_LIBRARY_PATH venv/bin/python -c "import xatlas, mesh_processor, custom_rasterizer; from hy3dgen.texgen import Hunyuan3DPaintPipeline"
```

4. Only AFTER the probe passes, run one manual `include_texture=true` smoke request.

Do NOT claim stable Linux ARM64 texgen support from a successful probe alone. A textured smoke run is still required.

## Known Limits / Non-Goals

This extension does **not** claim support for:

- CPU-only Linux inference
- stable texgen/custom rasterizer ARM64 support
- Modly core multi-input mapping fixes

Linux ARM64 texgen is currently **experimental and probe-gated**, not a stable support claim. Shape-only Linux ARM64 support remains the validated path.

See `MODLY_CORE_NOTES.md` for the current named-input workflow limitation.

## Manual Smoke Verification

Do NOT build anything for this checklist.

### Texgen Smoke Checklist

1. **Shape-only default**
   - Run one normal request with default params.
   - Confirm the result is the same shape-only GLB path and that no texgen capability failure appears.

2. **Explicit probe-failure path**
   - Set `include_texture=true` on a host missing CUDA/texgen deps/weights or without valid Hugging Face access.
   - Confirm the run fails with an actionable probe error listing the failed check(s), including `xatlas`, `custom_rasterizer`, and `mesh_processor` when they are the blockers, plus the required local paths.

3. **Supported CUDA texture success**
   - On a host with CUDA, `hy3dgen.texgen`, `xatlas`, `custom_rasterizer`, `mesh_processor`, and local `tencent/Hunyuan3D-2` paint/delight weights, run one request with:
     - `include_texture=true`
     - `texture_model_variant=turbo`
     - `texture_input_mode=front`
   - Confirm the final artifact is a textured GLB.

4. **Named-input validation after manifest update**
   - In Modly UI/workflow wiring, confirm inputs remain named `front`, `left`, `back`, and `right` after the new texture params are added.

### 1. Verify install package choices

Inside the extension venv, inspect installed packages and confirm:

- Linux ARM64 does **not** install `xformers`
- Linux ARM64 does **not** depend on `onnxruntime-gpu`
- Torch/Torchvision match the pinned target expected for the host

Example checks:

```bash
venv/bin/pip show torch torchvision rembg onnxruntime
venv/bin/pip show xformers onnxruntime-gpu
```

### 2. Verify weight download

- Trigger the extension/model download from Modly
- confirm model files land under the extension model directory
- confirm the selected variant contains `model.fp16.safetensors`

### 3. Verify generator load

- load the extension once
- confirm `hy3dgen` resolves from installed package, `Hunyuan3D-2`, or `_hy3dgen`
- if `_hy3dgen` was absent, confirm it gets created under the model cache

### 4. Verify background-removal fallback

- run one generation with `remove_bg=true`
- confirm Linux ARM64 can continue with CPU ONNX fallback if the default rembg provider path fails

### 5. Verify GLB export

- run one front-image generation
- confirm a `.glb` file is written to the Modly workspace output directory

## Validation Boundary

- Full Modly backend/MCP validation requires a healthy local Modly backend.
- If the backend is unavailable, validation is limited to local install/runtime smoke checks and static Windows regression review.
- When the backend is healthy, run the Modly install/apply path plus the smoke checklist above before archiving the change.

## Rollback

If this compatibility path must be reverted:

1. revert Linux ARM64 branches in `setup.py`
2. revert layered `_hy3dgen` loading and Linux rembg fallback in `generator.py`
3. remove Linux ARM64 support claims from `README.md`
4. delete any temporary `_hy3dgen` cache created during manual testing

## Developer Notes

See `MODLY_CORE_NOTES.md` for details on preserving named multi-image inputs. The extension is pre-configured to handle `left`, `back`, and `right` image paths once the core mapping is updated.
