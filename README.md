# Hunyuan3D-2mv Modly Extension

Multi-view image-to-mesh generation for Modly using Tencent Hunyuan3D-2mv.

## Supported Platforms

- **Windows**: existing install path remains supported.
- **Linux ARM64 + NVIDIA CUDA**: first supported Linux target in this change.
- **Other Linux/macOS targets**: not claimed as supported yet.

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

The installer now uses an explicit Linux ARM64 branch:

- installs **pinned** ARM64 CUDA wheels for Torch/Torchvision
- does **not** require `xformers`
- installs `rembg` + CPU `onnxruntime`
- does **not** require `onnxruntime-gpu`
- does **not** clone/install `Hunyuan3D-2` editable during setup
- lazily downloads/extracts `_hy3dgen` source on first runtime import if needed

Pinned Torch targets:

- **cu124**: `torch==2.5.1`, `torchvision==0.20.1`
- **cu128 / Blackwell-tier path**: `torch==2.7.0+cu128`, `torchvision==0.22.0`

If a pinned ARM64 wheel is unavailable for the embedded Python tag, setup now fails clearly instead of silently downgrading.

### Windows / non-ARM64

The prior behavior is intentionally preserved as much as possible:

- existing GPU-SM-based Torch selection remains
- `xformers` stays in the install path
- editable `Hunyuan3D-2` repo install remains the compatibility path
- `onnxruntime-gpu` is still attempted where it was already used

## Runtime Behavior

`generator.py` now resolves `hy3dgen` in this order:

1. already-installed `hy3dgen`
2. extension-local `Hunyuan3D-2/`
3. model cache `_hy3dgen/`

On Linux ARM64, if `_hy3dgen` is missing, the generator downloads the upstream GitHub zip and extracts only the `hy3dgen/` tree into the model cache.

Background removal behavior:

- default path still uses upstream `BackgroundRemover`
- on Linux ARM64 failure, runtime retries with `rembg.new_session(..., providers=["CPUExecutionProvider"])`
- if both fail, generation continues with the original image and logs the reason

## Known Limits / Non-Goals

This change does **not** claim support for:

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

Until Modly maps model inputs by `targetHandle`, connected workflow images can still collapse to one primary front image. The extension already accepts optional side views when Modly passes `left_image_path`, `back_image_path`, and `right_image_path` params.
