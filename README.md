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

## Known Limits / Non-Goals

This extension does **not** claim support for:

- CPU-only Linux inference
- texgen/custom rasterizer ARM64 support
- Modly core multi-input mapping fixes

See `MODLY_CORE_NOTES.md` for the current named-input workflow limitation.

## Manual Smoke Verification

Do NOT build anything for this checklist.

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
