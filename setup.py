"""
Hunyuan3D-2mv - Modly extension setup script.

Called by Modly at install time:
    python setup.py <json_args>

json_args keys:
    python_exe     - path to Modly's embedded Python
    ext_dir        - absolute path to this extension directory
    gpu_sm         - GPU compute capability as integer (e.g. 89 for RTX 4050)
    cuda_version   - optional CUDA major/minor encoded as integer (e.g. 124, 128)
"""
import json
import platform
import subprocess
import sys
from pathlib import Path


ARM64_CU124_WHEELS = {
    "cp39": {
        "torch": "https://download-r2.pytorch.org/whl/cu124/torch-2.5.1-cp39-cp39-linux_aarch64.whl#sha256=012887a6190e562cb266d2210052c5deb5113f520a46dc2beaa57d76144a0e9b",
        "torchvision": "https://download-r2.pytorch.org/whl/cu124/torchvision-0.20.1-cp39-cp39-linux_aarch64.whl#sha256=e25b4ac3c9eec3f789f1c5491331dfe236b5f06a1f406ea82fa59fed4fc6f71e",
    },
    "cp310": {
        "torch": "https://download-r2.pytorch.org/whl/cu124/torch-2.5.1-cp310-cp310-linux_aarch64.whl#sha256=d468d0eddc188aa3c1e417ec24ce615c48c0c3f592b0354d9d3b99837ef5faa6",
        "torchvision": "https://download-r2.pytorch.org/whl/cu124/torchvision-0.20.1-cp310-cp310-linux_aarch64.whl#sha256=38765e53653f93e529e329755992ddbea81091aacedb61ed053f6a14efb289e5",
    },
    "cp311": {
        "torch": "https://download-r2.pytorch.org/whl/cu124/torch-2.5.1-cp311-cp311-linux_aarch64.whl#sha256=e080353c245b752cd84122e4656261eee6d4323a37cfb7d13e0fffd847bae1a3",
        "torchvision": "https://download-r2.pytorch.org/whl/cu124/torchvision-0.20.1-cp311-cp311-linux_aarch64.whl#sha256=2c5350a08abe005a16c316ae961207a409d0e35df86240db5f77ec41345c82f3",
    },
    "cp312": {
        "torch": "https://download-r2.pytorch.org/whl/cu124/torch-2.5.1-cp312-cp312-linux_aarch64.whl#sha256=302041d457ee169fd925b53da283c13365c6de75c6bb3e84130774b10e2fbb39",
        "torchvision": "https://download-r2.pytorch.org/whl/cu124/torchvision-0.20.1-cp312-cp312-linux_aarch64.whl#sha256=3e3289e53d0cb5d1b7f55b3f5912f46a08293c6791585ba2fc32c12cded9f9af",
    },
}

ARM64_CU128_WHEELS = {
    "cp39": {
        "torch": "https://download-r2.pytorch.org/whl/cu128/torch-2.7.0%2Bcu128-cp39-cp39-manylinux_2_28_aarch64.whl#sha256=2f155388b1200e08f3e901bb3487ff93ca6d63cde87c29b97bb6762a8f63b373",
        "torchvision": "https://download-r2.pytorch.org/whl/cu128/torchvision-0.22.0-cp39-cp39-manylinux_2_28_aarch64.whl#sha256=7a398fad02f4ac6b7d18bea9a08dc14163ffc5a368618f29ceb0e53dfa91f69e",
    },
    "cp310": {
        "torch": "https://download-r2.pytorch.org/whl/cu128/torch-2.7.0%2Bcu128-cp310-cp310-manylinux_2_28_aarch64.whl#sha256=b1f0cdd0720ad60536deb5baa427b782fd920dd4fcf72e244d32974caafa3b9e",
        "torchvision": "https://download-r2.pytorch.org/whl/cu128/torchvision-0.22.0-cp310-cp310-manylinux_2_28_aarch64.whl#sha256=566224d7b4f00bc6366bed1d62f834ca80f8e57fe41e10e4a5636bfa3ffb984e",
    },
    "cp311": {
        "torch": "https://download-r2.pytorch.org/whl/cu128/torch-2.7.0%2Bcu128-cp311-cp311-manylinux_2_28_aarch64.whl#sha256=47c895bcab508769d129d717a4b916b10225ae3855723aeec8dff8efe5346207",
        "torchvision": "https://download-r2.pytorch.org/whl/cu128/torchvision-0.22.0-cp311-cp311-manylinux_2_28_aarch64.whl#sha256=6be714bcdd8849549571f6acfaa2dfa9e00676f042bda517432745fb116f7904",
    },
    "cp312": {
        "torch": "https://download-r2.pytorch.org/whl/cu128/torch-2.7.0%2Bcu128-cp312-cp312-manylinux_2_28_aarch64.whl#sha256=6bba7dca5d9a729f1e8e9befb98055498e551efaf5ed034824c168b560afc1ac",
        "torchvision": "https://download-r2.pytorch.org/whl/cu128/torchvision-0.22.0-cp312-cp312-manylinux_2_28_aarch64.whl#sha256=6e9752b48c1cdd7f6428bcd30c3d198b30ecea348d16afb651f95035e5252506",
    },
    "cp313": {
        "torch": "https://download-r2.pytorch.org/whl/cu128/torch-2.7.0%2Bcu128-cp313-cp313-manylinux_2_28_aarch64.whl#sha256=633f35e8b1b1f640ef5f8a98dbd84f19b548222ce7ba8f017fe47ce6badc106a",
        "torchvision": "https://download-r2.pytorch.org/whl/cu128/torchvision-0.22.0-cp313-cp313-manylinux_2_28_aarch64.whl#sha256=e4d4d5a14225875d9bf8c5221d43d8be97786adc498659493799bdeff52c54cf",
    },
}


def pip(venv, *args):
    is_win = platform.system() == "Windows"
    pip_exe = venv / ("Scripts/pip.exe" if is_win else "bin/pip")
    subprocess.run([str(pip_exe)] + list(args), check=True)


def python_exe_in_venv(venv):
    is_win = platform.system() == "Windows"
    return venv / ("Scripts/python.exe" if is_win else "bin/python")


def python_tag(venv):
    python_exe = python_exe_in_venv(venv)
    return subprocess.check_output(
        [str(python_exe), "-c", "import sys; print(f'cp{sys.version_info.major}{sys.version_info.minor}')"],
        text=True,
    ).strip()


def detect_gpu_sm():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            check=True,
            capture_output=True,
            text=True,
        )
        raw = result.stdout.strip().splitlines()[0].strip()
        major, minor = raw.split(".", 1)
        return int(major) * 10 + int(minor)
    except Exception as exc:
        raise RuntimeError(
            "gpu_sm is required when automatic GPU detection is unavailable. "
            "Provide it via setup payload or run on a machine with nvidia-smi."
        ) from exc


def resolve_gpu_sm(args):
    if "gpu_sm" in args and args["gpu_sm"] is not None:
        return int(args["gpu_sm"])
    return detect_gpu_sm()


def resolve_cuda_version(args):
    value = args.get("cuda_version") if isinstance(args, dict) else None
    return int(value) if value not in (None, "") else 0


def install_arm64_pytorch(venv, wheel_map, label, extra_index_url):
    py_tag = python_tag(venv)
    wheel_urls = wheel_map.get(py_tag)
    if wheel_urls is None:
        raise RuntimeError(
            "Unsupported Python version for Linux ARM64 pinned PyTorch wheels: %s. "
            "Supported tags: %s"
            % (py_tag, ", ".join(sorted(wheel_map)))
        )

    print("[setup] Installing pinned Linux ARM64 wheels for %s (%s)..." % (label, py_tag))
    pip(
        venv,
        "install",
        "--retries",
        "10",
        "--timeout",
        "120",
        "--no-cache-dir",
        "--extra-index-url",
        extra_index_url,
        wheel_urls["torch"],
        wheel_urls["torchvision"],
    )


def install_pytorch(venv, gpu_sm, cuda_version, is_linux_arm64):
    if is_linux_arm64 and (gpu_sm >= 100 or cuda_version >= 128):
        print("[setup] Linux ARM64 + NVIDIA CUDA -> pinned PyTorch 2.7.0 / torchvision 0.22.0 (cu128)")
        install_arm64_pytorch(venv, ARM64_CU128_WHEELS, "cu128", "https://download.pytorch.org/whl/cu128")
        return "linux-arm64-cu128"

    if is_linux_arm64 and gpu_sm >= 70:
        print("[setup] Linux ARM64 + NVIDIA CUDA -> pinned PyTorch 2.5.1 / torchvision 0.20.1 (cu124)")
        install_arm64_pytorch(venv, ARM64_CU124_WHEELS, "cu124", "https://download.pytorch.org/whl/cu124")
        return "linux-arm64-cu124"

    if is_linux_arm64:
        raise RuntimeError(
            "Linux ARM64 support currently targets NVIDIA CUDA hosts with GPU SM >= 70. "
            "Received gpu_sm=%s and cuda_version=%s." % (gpu_sm, cuda_version)
        )

    if gpu_sm >= 100:
        torch_index = "https://download.pytorch.org/whl/cu128"
        torch_pkgs = ["torch>=2.7.0", "torchvision>=0.22.0", "torchaudio>=2.7.0"]
        print("[setup] SM %d (Blackwell) -> PyTorch 2.7 + CUDA 12.8" % gpu_sm)
    elif gpu_sm >= 70:
        torch_index = "https://download.pytorch.org/whl/cu124"
        torch_pkgs = ["torch==2.5.1", "torchvision==0.20.1", "torchaudio==2.5.1"]
        print("[setup] SM %d -> PyTorch 2.5.1 + CUDA 12.4" % gpu_sm)
    else:
        torch_index = "https://download.pytorch.org/whl/cu118"
        torch_pkgs = ["torch==2.5.1", "torchvision==0.20.1", "torchaudio==2.5.1"]
        print("[setup] SM %d (legacy) -> PyTorch 2.5.1 + CUDA 11.8" % gpu_sm)

    print("[setup] Installing PyTorch...")
    pip(venv, "install", *torch_pkgs, "--index-url", torch_index)
    return "default"


def install_xformers(venv, gpu_sm, is_linux_arm64):
    if is_linux_arm64:
        print("[setup] Skipping xformers on Linux ARM64 (not required for the supported path).")
        return

    print("[setup] Installing xformers...")
    if gpu_sm >= 70:
        pip(venv, "install", "xformers==0.0.28.post3", "--index-url", "https://download.pytorch.org/whl/cu124")
    else:
        pip(
            venv,
            "install",
            "xformers==0.0.28.post2",
            "--index-url",
            "https://download.pytorch.org/whl/cu118",
        )


def prepare_hy3dgen_source(ext_dir, venv, is_linux_arm64):
    if is_linux_arm64:
        print("[setup] Linux ARM64 will use lazy _hy3dgen source loading at runtime; skipping git clone/editable install.")
        return

    repo_dir = ext_dir / "Hunyuan3D-2"
    if not repo_dir.exists():
        print("[setup] Cloning Hunyuan3D-2 repo...")
        subprocess.run(
            [
                "git",
                "clone",
                "--depth=1",
                "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.git",
                str(repo_dir),
            ],
            check=True,
        )
    else:
        print("[setup] Repo already exists, skipping clone.")

    print("[setup] Installing hy3dgen package...")
    venv_python = python_exe_in_venv(venv)
    subprocess.run([str(venv_python), "-m", "pip", "install", "-e", str(repo_dir)], check=True)


def install_core_dependencies(venv):
    print("[setup] Installing core dependencies...")
    pip(
        venv,
        "install",
        "transformers==4.40.2",
        "diffusers==0.27.2",
        "huggingface_hub==0.23.5",
        "accelerate",
        "omegaconf",
        "einops",
        "Pillow",
        "numpy",
        "scipy",
        "trimesh",
        "pymeshlab",
        "pygltflib",
        "opencv-python-headless",
        "tqdm",
        "safetensors",
    )


def install_background_removal_dependencies(venv, gpu_sm, is_linux_arm64):
    if is_linux_arm64:
        print("[setup] Installing Linux ARM64 rembg + CPU onnxruntime...")
        pip(venv, "install", "rembg", "onnxruntime")
        return

    print("[setup] Installing rembg + onnxruntime...")
    pip(venv, "install", "rembg", "onnxruntime")

    if gpu_sm >= 70:
        print("[setup] Installing onnxruntime-gpu...")
        try:
            pip(venv, "install", "onnxruntime-gpu")
        except subprocess.CalledProcessError:
            print("[setup] onnxruntime-gpu failed, keeping CPU onnxruntime.")


def setup(python_exe, ext_dir, gpu_sm, cuda_version=0):
    venv = ext_dir / "venv"
    machine = platform.machine().lower()
    is_linux_arm64 = platform.system() == "Linux" and machine in {"aarch64", "arm64"}

    print("[setup] Creating venv at %s ..." % venv)
    subprocess.run([python_exe, "-m", "venv", str(venv)], check=True)

    install_pytorch(venv, gpu_sm, cuda_version, is_linux_arm64)
    install_xformers(venv, gpu_sm, is_linux_arm64)
    prepare_hy3dgen_source(ext_dir, venv, is_linux_arm64)
    install_core_dependencies(venv)
    install_background_removal_dependencies(venv, gpu_sm, is_linux_arm64)

    print("[setup] Done. Venv ready at: %s" % venv)


if __name__ == "__main__":
    if len(sys.argv) >= 4:
        setup(
            python_exe=sys.argv[1],
            ext_dir=Path(sys.argv[2]),
            gpu_sm=int(sys.argv[3]),
            cuda_version=int(sys.argv[4]) if len(sys.argv) >= 5 else 0,
        )
    elif len(sys.argv) == 2:
        args = json.loads(sys.argv[1])
        setup(
            python_exe=args["python_exe"],
            ext_dir=Path(args["ext_dir"]),
            gpu_sm=resolve_gpu_sm(args),
            cuda_version=resolve_cuda_version(args),
        )
    else:
        print("Usage: python setup.py <python_exe> <ext_dir> <gpu_sm> [cuda_version]")
        print('   or: python setup.py \'{"python_exe":"...","ext_dir":"...","gpu_sm":89,"cuda_version":128}\'')
        sys.exit(1)
