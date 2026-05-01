#!/usr/bin/env python3
"""Prepare optional Linux ARM64 texgen runtime pieces inside an existing venv.

This script is intentionally OPERATOR-ONLY. It does not change setup.py defaults
and it refuses to touch global/system Python targets.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VENV = REPO_ROOT / "venv"
DEFAULT_SOURCE_CANDIDATES = (
    REPO_ROOT / "Hunyuan3D-2",
    REPO_ROOT / "Hunyuan3D-2-main",
)
SCRIPT_TEMP_ROOT = REPO_ROOT / ".texgen-runtime-prep"
STAGES = ("inspect", "xatlas", "mesh_processor", "custom_rasterizer", "probe", "all")
SUPPORTED_PYTHON_TAGS = {"cp312"}
EXPECTED_TORCH_CUDA_SUFFIXES = {"cu128", "cu124"}
BUILD_HELPERS = (
    "pip",
    "setuptools",
    "wheel",
    "build",
    "packaging",
    "ninja",
    "pybind11",
    "scikit-build-core",
)


@dataclass
class CommandPlan:
    label: str
    argv: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    unset_env: List[str] = field(default_factory=list)
    cwd: Optional[Path] = None
    action: str = "command"


@dataclass
class SourceLayout:
    root: Path
    pythonpath_entry: Path
    hy3dgen_dir: Path
    texgen_dir: Path
    mesh_processor_dir: Path
    custom_rasterizer_dir: Path


@dataclass
class PrepContext:
    repo_root: Path
    venv_path: Path
    venv_python: Path
    venv_pip: Path
    temp_root: Path
    stage: str
    dry_run: bool
    clean: bool
    arch_list: Optional[str]
    source_layout: Optional[SourceLayout]
    xatlas_artifact: Optional[Path]
    mesh_processor_artifact: Optional[Path]
    custom_rasterizer_artifact: Optional[Path]
    checks: List[Dict[str, object]] = field(default_factory=list)
    planned_commands: List[CommandPlan] = field(default_factory=list)
    cleanup_notes: List[str] = field(default_factory=list)
    rollback_notes: List[str] = field(default_factory=list)
    refusals: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    torch_info: Dict[str, object] = field(default_factory=dict)
    detected_sm: Optional[str] = None
    cuda_home: Optional[str] = None
    nvcc_path: Optional[Path] = None
    nvcc_version: Optional[str] = None
    torch_lib_path: Optional[Path] = None
    site_packages: List[Path] = field(default_factory=list)
    patchelf_path: Optional[Path] = None
    custom_rasterizer_kernels: List[Path] = field(default_factory=list)
    build_helpers_bootstrap_planned: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", default=str(DEFAULT_VENV), help="Target extension venv (default: %(default)s)")
    parser.add_argument("--source-root", help="Hunyuan3D-2 source root containing hy3dgen/texgen")
    parser.add_argument("--stage", choices=STAGES, default="inspect", help="Stage to inspect/prepare")
    parser.add_argument("--dry-run", action="store_true", help="Print checks and planned commands without mutating")
    parser.add_argument("--clean", action="store_true", help="Remove script-owned temp/build dirs before running")
    parser.add_argument("--arch-list", help="Explicit TORCH_CUDA_ARCH_LIST override, e.g. '12.0+PTX' for GB10/SM 12.1 with Torch 2.7.0+cu128")
    parser.add_argument("--xatlas-wheel", help="Optional xatlas wheel or sdist path")
    parser.add_argument("--mesh-processor-wheel", help="Optional mesh_processor wheel path")
    parser.add_argument("--custom-rasterizer-wheel", help="Optional custom_rasterizer wheel path")
    return parser.parse_args()


def add_check(ctx: PrepContext, name: str, ok: bool, detail: str) -> None:
    ctx.checks.append({"name": name, "ok": ok, "detail": detail})


def add_refusal(ctx: PrepContext, message: str) -> None:
    if message not in ctx.refusals:
        ctx.refusals.append(message)


def normalize_machine(value: str) -> str:
    text = (value or "").strip().lower()
    aliases = {
        "arm64": "aarch64",
        "armv8l": "aarch64",
    }
    return aliases.get(text, text)


def quoted(argv: Sequence[str]) -> str:
    return shlex.join([str(item) for item in argv])


def env_prefix(env: Dict[str, str]) -> str:
    if not env:
        return ""
    ordered = [f"{key}={shlex.quote(str(value))}" for key, value in sorted(env.items())]
    return " ".join(ordered) + " "


def command_prefix(plan: CommandPlan) -> str:
    unset = " ".join(f"-u {shlex.quote(key)}" for key in plan.unset_env)
    unset_prefix = f"env {unset} " if unset else ""
    return unset_prefix + env_prefix(plan.env)


def run_capture(argv: Sequence[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [str(item) for item in argv],
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


def is_executable_file(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.X_OK)


def same_resolved_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()


def merge_library_path_entries(*groups: Sequence[str]) -> List[str]:
    seen = set()
    merged: List[str] = []
    for group in groups:
        for raw in group:
            value = str(raw or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            merged.append(value)
    return merged


def ensure_venv_paths(venv_path: Path) -> Tuple[Path, Path]:
    venv_python = venv_path / "bin" / "python"
    venv_pip = venv_path / "bin" / "pip"
    return venv_python, venv_pip


def resolve_source_layout(source_root: Optional[str]) -> Optional[SourceLayout]:
    candidates: List[Path] = []
    if source_root:
        candidates.append(Path(source_root).expanduser().resolve())
    else:
        candidates.extend(path.resolve() for path in DEFAULT_SOURCE_CANDIDATES if path.exists())

    for candidate in candidates:
        direct_hy3dgen = candidate / "hy3dgen"
        if direct_hy3dgen.exists():
            hy3dgen_dir = direct_hy3dgen
            pythonpath_entry = candidate
            root = candidate
        elif candidate.name == "hy3dgen" and candidate.exists():
            hy3dgen_dir = candidate
            pythonpath_entry = candidate.parent
            root = candidate.parent
        else:
            continue

        texgen_dir = hy3dgen_dir / "texgen"
        mesh_processor_dir = texgen_dir / "differentiable_renderer"
        custom_rasterizer_dir = texgen_dir / "custom_rasterizer"
        if texgen_dir.exists() and mesh_processor_dir.exists() and custom_rasterizer_dir.exists():
            return SourceLayout(
                root=root,
                pythonpath_entry=pythonpath_entry,
                hy3dgen_dir=hy3dgen_dir,
                texgen_dir=texgen_dir,
                mesh_processor_dir=mesh_processor_dir,
                custom_rasterizer_dir=custom_rasterizer_dir,
            )
    return None


def detect_python_tag(ctx: PrepContext) -> Optional[str]:
    result = run_capture([ctx.venv_python, "-c", "import sys; print(f'cp{sys.version_info.major}{sys.version_info.minor}')"])
    if result.returncode != 0:
        add_check(ctx, "target-python", False, (result.stderr or result.stdout).strip() or "unable to query target python")
        add_refusal(ctx, "Unable to query target venv Python version.")
        return None
    py_tag = result.stdout.strip()
    ok = py_tag in SUPPORTED_PYTHON_TAGS
    add_check(ctx, "target-python", ok, f"detected {py_tag}; supported={sorted(SUPPORTED_PYTHON_TAGS)}")
    if not ok:
        add_refusal(ctx, f"Unsupported Python ABI {py_tag}. This workflow currently targets cp312 only.")
    return py_tag


def detect_torch(ctx: PrepContext) -> Dict[str, object]:
    code = (
        "import json\n"
        "from pathlib import Path\n"
        "try:\n"
        " import torch\n"
        " info = {\n"
        "   'ok': True,\n"
        "   'version': getattr(torch, '__version__', ''),\n"
        "   'cuda': getattr(torch.version, 'cuda', None),\n"
        "   'cuda_available': bool(torch.cuda.is_available()),\n"
        "   'torch_lib': str((Path(torch.__file__).resolve().parent / 'lib')),\n"
        " }\n"
        " if torch.cuda.is_available():\n"
        "  major, minor = torch.cuda.get_device_capability(0)\n"
        "  info['sm'] = f'{major}.{minor}'\n"
        " print(json.dumps(info))\n"
        "except Exception as exc:\n"
        " print(json.dumps({'ok': False, 'error': str(exc)}))\n"
    )
    result = run_capture([ctx.venv_python, "-c", code])
    if result.returncode != 0:
        add_check(ctx, "torch", False, (result.stderr or result.stdout).strip() or "unable to query torch")
        add_refusal(ctx, "Unable to inspect torch in the target venv.")
        return {}
    try:
        info = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        add_check(ctx, "torch", False, result.stdout.strip() or "unparseable torch inspection output")
        add_refusal(ctx, "Torch inspection produced invalid output.")
        return {}

    if not info.get("ok"):
        detail = str(info.get("error") or "torch is not importable")
        add_check(ctx, "torch", False, detail)
        add_refusal(ctx, f"Torch is missing or not importable in the target venv: {detail}")
        return info

    version = str(info.get("version") or "")
    cuda = str(info.get("cuda") or "")
    suffix = f"cu{cuda.replace('.', '')}" if cuda else ""
    if not suffix and "+" in version:
        suffix = version.rsplit("+", 1)[-1]
    version_ok = suffix in EXPECTED_TORCH_CUDA_SUFFIXES
    detail = f"version={version} cuda={cuda or 'none'} suffix={suffix or 'none'}"
    add_check(ctx, "torch", version_ok, detail)
    if not version_ok:
        add_refusal(
            ctx,
            "Torch runtime is not on an expected Linux ARM64 CUDA build. "
            f"Expected one of {sorted(EXPECTED_TORCH_CUDA_SUFFIXES)}, got {suffix or 'none'} ({version}).",
        )

    sm_value = info.get("sm")
    if sm_value:
        ctx.detected_sm = str(sm_value)
        add_check(ctx, "gpu-sm", True, f"torch reports SM {ctx.detected_sm}")
    else:
        add_check(ctx, "gpu-sm", False, "torch could not report compute capability; will try nvidia-smi")

    torch_lib = info.get("torch_lib")
    if torch_lib:
        torch_lib_path = Path(str(torch_lib)).expanduser()
        ctx.torch_lib_path = torch_lib_path
        add_check(ctx, "torch-lib", torch_lib_path.exists(), str(torch_lib_path))
        if not torch_lib_path.exists():
            ctx.warnings.append(
                f"Torch import succeeded but expected torch runtime lib dir does not exist: {torch_lib_path}"
            )
    return info


def detect_nvcc(ctx: PrepContext) -> None:
    path_nvcc_raw = shutil.which("nvcc")
    path_nvcc = Path(path_nvcc_raw).resolve() if path_nvcc_raw else None
    cuda_home_env = os.environ.get("CUDA_HOME")
    cuda_path_env = os.environ.get("CUDA_PATH")
    selected_cuda_home = cuda_home_env or cuda_path_env

    if cuda_home_env and cuda_path_env:
        cuda_home_resolved = Path(cuda_home_env).expanduser().resolve()
        cuda_path_resolved = Path(cuda_path_env).expanduser().resolve()
        if cuda_home_resolved != cuda_path_resolved:
            ctx.warnings.append(
                f"CUDA_HOME ({cuda_home_resolved}) and CUDA_PATH ({cuda_path_resolved}) differ; selecting CUDA_HOME."
            )

    selected_nvcc: Optional[Path] = None
    if selected_cuda_home:
        cuda_home_path = Path(selected_cuda_home).expanduser().resolve()
        selected_cuda_home = str(cuda_home_path)
        home_nvcc = cuda_home_path / "bin" / "nvcc"
        if is_executable_file(home_nvcc):
            selected_nvcc = home_nvcc
            add_check(ctx, "CUDA home nvcc", True, f"selected {home_nvcc}")
            if path_nvcc and not same_resolved_path(path_nvcc, home_nvcc):
                ctx.warnings.append(
                    f"PATH nvcc ({path_nvcc}) differs from selected CUDA home nvcc ({home_nvcc}); ignoring PATH nvcc for version detection and CUDA stage planning."
                )
        else:
            add_check(ctx, "CUDA home nvcc", False, f"not found/executable at {home_nvcc}")
            if path_nvcc:
                selected_nvcc = path_nvcc
                ctx.warnings.append(
                    f"Selected CUDA home {cuda_home_path} has no executable bin/nvcc; falling back to PATH nvcc ({path_nvcc})."
                )
    elif path_nvcc:
        selected_nvcc = path_nvcc
        selected_cuda_home = str(path_nvcc.parent.parent)

    if path_nvcc:
        add_check(ctx, "PATH nvcc", True, str(path_nvcc))
    else:
        add_check(ctx, "PATH nvcc", False, "nvcc not found in PATH")

    if selected_nvcc:
        result = run_capture([selected_nvcc, "--version"])
        detail = (result.stdout or result.stderr).strip().replace("\n", " | ")
        ok = result.returncode == 0
        add_check(ctx, "selected nvcc", ok, f"{selected_nvcc}: {detail or '<no version output>'}")
        if ok:
            ctx.nvcc_path = selected_nvcc
            ctx.nvcc_version = detail
    else:
        add_check(ctx, "selected nvcc", False, "no nvcc available from selected CUDA home or PATH fallback")
    ctx.cuda_home = selected_cuda_home
    add_check(ctx, "CUDA home", bool(selected_cuda_home), selected_cuda_home or "CUDA_HOME/CUDA_PATH not set and nvcc not found")
    if selected_cuda_home:
        cuda_lib64 = Path(selected_cuda_home).expanduser() / "lib64"
        add_check(ctx, "CUDA lib64", cuda_lib64.exists(), str(cuda_lib64))


def detect_site_packages(ctx: PrepContext) -> None:
    code = (
        "import json, site, sysconfig\n"
        "paths = []\n"
        "for value in site.getsitepackages() + [sysconfig.get_path('purelib'), sysconfig.get_path('platlib')]:\n"
        "    if value and value not in paths:\n"
        "        paths.append(value)\n"
        "print(json.dumps(paths))\n"
    )
    result = run_capture([ctx.venv_python, "-c", code])
    if result.returncode != 0:
        add_check(ctx, "site-packages", False, (result.stderr or result.stdout).strip() or "unable to query target site-packages")
        ctx.warnings.append("Unable to query target venv site-packages; custom_rasterizer RUNPATH patch cannot locate installed kernels yet.")
        return
    try:
        raw_paths = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError:
        add_check(ctx, "site-packages", False, result.stdout.strip() or "unparseable site-packages output")
        ctx.warnings.append("Target venv site-packages query produced invalid output; custom_rasterizer RUNPATH patch cannot locate installed kernels yet.")
        return
    ctx.site_packages = []
    for raw in raw_paths:
        path = Path(str(raw)).expanduser()
        if path not in ctx.site_packages:
            ctx.site_packages.append(path)
    existing = [path for path in ctx.site_packages if path.exists()]
    add_check(ctx, "site-packages", bool(existing), ", ".join(str(path) for path in ctx.site_packages) or "none")


def detect_patchelf(ctx: PrepContext) -> None:
    venv_patchelf = ctx.venv_path / "bin" / "patchelf"
    if venv_patchelf.exists() and os.access(venv_patchelf, os.X_OK):
        ctx.patchelf_path = venv_patchelf
        add_check(ctx, "patchelf", True, f"venv-local {venv_patchelf}")
        return
    path_patchelf = shutil.which("patchelf")
    if path_patchelf:
        ctx.patchelf_path = Path(path_patchelf)
        add_check(ctx, "patchelf", True, f"PATH fallback {path_patchelf}")
        return
    add_check(ctx, "patchelf", False, f"not found at {venv_patchelf} or in PATH")


def locate_custom_rasterizer_kernels(ctx: PrepContext) -> List[Path]:
    kernels: List[Path] = []
    seen = set()
    for site_dir in ctx.site_packages:
        if not site_dir.exists():
            continue
        for candidate in site_dir.rglob("custom_rasterizer_kernel*.so"):
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            kernels.append(resolved)
    return sorted(kernels)


def refresh_custom_rasterizer_kernels(ctx: PrepContext) -> None:
    ctx.custom_rasterizer_kernels = locate_custom_rasterizer_kernels(ctx)
    if ctx.custom_rasterizer_kernels:
        add_check(
            ctx,
            "custom_rasterizer kernels",
            True,
            ", ".join(str(path) for path in ctx.custom_rasterizer_kernels),
        )
    else:
        detail = "no custom_rasterizer_kernel*.so found yet in target venv site-packages"
        add_check(ctx, "custom_rasterizer kernels", True, detail)


def custom_rasterizer_runpath(ctx: PrepContext) -> str:
    return os.pathsep.join(runtime_library_paths(ctx))


def patchelf_print_rpath(ctx: PrepContext, shared_object: Path) -> str:
    if ctx.patchelf_path is None:
        return ""
    result = run_capture([ctx.patchelf_path, "--print-rpath", shared_object])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def validate_custom_rasterizer_runpath(ctx: PrepContext, shared_object: Path, expected_entries: Sequence[str]) -> None:
    current = patchelf_print_rpath(ctx, shared_object)
    current_entries = [entry for entry in current.split(os.pathsep) if entry]
    missing = [entry for entry in expected_entries if entry not in current_entries]
    if missing:
        raise RuntimeError(f"RUNPATH verification failed for {shared_object}; missing {missing}; current={current or '<empty>'}")


def patch_custom_rasterizer_runpath(ctx: PrepContext) -> None:
    runpath_entries = runtime_library_paths(ctx)
    if not has_required_runpath_entries(ctx):
        raise RuntimeError("Cannot patch custom_rasterizer RUNPATH because both target venv Torch lib and CUDA lib64 paths are required.")
    if ctx.patchelf_path is None:
        raise RuntimeError("Cannot patch custom_rasterizer RUNPATH because patchelf was not found in the venv or PATH.")
    refresh_custom_rasterizer_kernels(ctx)
    if not ctx.custom_rasterizer_kernels:
        raise RuntimeError("Cannot patch custom_rasterizer RUNPATH because no custom_rasterizer_kernel*.so was found in the target venv site-packages.")
    runpath = os.pathsep.join(runpath_entries)
    for shared_object in ctx.custom_rasterizer_kernels:
        print(f"[run] patch RUNPATH for {shared_object}")
        subprocess.run([str(ctx.patchelf_path), "--set-rpath", runpath, str(shared_object)], check=True)
        validate_custom_rasterizer_runpath(ctx, shared_object, runpath_entries)


def detect_nvidia_smi_sm(ctx: PrepContext) -> None:
    if ctx.detected_sm:
        return
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        add_check(ctx, "nvidia-smi", False, "nvidia-smi not found in PATH")
        return
    result = run_capture([nvidia_smi, "--query-gpu=compute_cap", "--format=csv,noheader"])
    if result.returncode != 0:
        add_check(ctx, "nvidia-smi", False, (result.stderr or result.stdout).strip() or "failed to query GPU SM")
        return
    raw = (result.stdout.strip().splitlines() or [""])[0].strip()
    if raw:
        ctx.detected_sm = raw
        add_check(ctx, "nvidia-smi", True, f"compute capability={raw}")
    else:
        add_check(ctx, "nvidia-smi", False, "no GPU compute capability reported")


def inspect_environment(ctx: PrepContext) -> None:
    system_ok = platform.system() == "Linux"
    machine = normalize_machine(platform.machine())
    arch_ok = machine == "aarch64"
    add_check(ctx, "platform", system_ok and arch_ok, f"system={platform.system()} machine={machine}")
    if not system_ok or not arch_ok:
        add_refusal(ctx, f"This workflow only supports Linux ARM64/aarch64 hosts. Detected {platform.system()} {machine}.")

    pyvenv_cfg = ctx.venv_path / "pyvenv.cfg"
    venv_ok = ctx.venv_path.exists() and pyvenv_cfg.exists() and ctx.venv_python.exists() and ctx.venv_pip.exists()
    add_check(
        ctx,
        "target-venv",
        venv_ok,
        f"path={ctx.venv_path} pyvenv.cfg={'yes' if pyvenv_cfg.exists() else 'no'} python={'yes' if ctx.venv_python.exists() else 'no'} pip={'yes' if ctx.venv_pip.exists() else 'no'}",
    )
    if not venv_ok:
        add_refusal(ctx, f"Target path {ctx.venv_path} is not a valid venv with pyvenv.cfg, bin/python, and bin/pip.")
        return

    detect_python_tag(ctx)
    ctx.torch_info = detect_torch(ctx)
    detect_nvcc(ctx)
    detect_site_packages(ctx)
    detect_patchelf(ctx)
    refresh_custom_rasterizer_kernels(ctx)
    detect_nvidia_smi_sm(ctx)

    if ctx.source_layout is None:
        add_check(
            ctx,
            "source-root",
            False,
            "No Hunyuan3D-2 source root detected. Pass --source-root PATH when mesh/custom stages are needed.",
        )
    else:
        add_check(
            ctx,
            "source-root",
            True,
            f"root={ctx.source_layout.root} texgen={ctx.source_layout.texgen_dir}",
        )


def stage_requires_source(stage: str) -> bool:
    return stage in {"mesh_processor", "custom_rasterizer", "all"}


def stage_requires_cuda(stage: str) -> bool:
    return stage in {"custom_rasterizer", "all"}


def require_mutation_inputs(ctx: PrepContext) -> None:
    if ctx.stage == "inspect" or ctx.stage == "probe":
        return
    if stage_requires_source(ctx.stage) and ctx.source_layout is None:
        add_refusal(ctx, "Missing --source-root (or detectable Hunyuan3D-2 checkout) for mesh/custom texgen stages.")
    if stage_requires_cuda(ctx.stage) and not ctx.nvcc_path:
        add_refusal(ctx, "custom_rasterizer stage requires nvcc from selected CUDA_HOME/CUDA_PATH or PATH fallback before mutation.")
    if stage_requires_cuda(ctx.stage) and not ctx.detected_sm and not ctx.arch_list:
        add_refusal(ctx, "custom_rasterizer stage requires --arch-list or detectable GPU compute capability.")
    if stage_requires_cuda(ctx.stage) and not has_required_runpath_entries(ctx):
        add_refusal(ctx, "custom_rasterizer stage requires detectable target venv Torch lib and CUDA lib64 paths for RUNPATH patching.")
    if stage_requires_cuda(ctx.stage) and ctx.patchelf_path is None:
        add_refusal(ctx, "custom_rasterizer stage requires patchelf in the target venv bin/ or PATH for RUNPATH patching.")


def artifact_platform_detail(path: Path) -> str:
    name = path.name.lower()
    if "win_amd64" in name or "win32" in name:
        return "windows"
    if "macosx" in name or "universal2" in name:
        return "macos"
    if "x86_64" in name or "amd64" in name:
        return "x86_64"
    if "aarch64" in name or "arm64" in name:
        return "aarch64"
    return "unknown"


def validate_artifact(ctx: PrepContext, label: str, path: Optional[Path], requires_torch_runtime_hint: bool) -> None:
    if path is None:
        return
    if not path.exists():
        add_refusal(ctx, f"{label} artifact does not exist: {path}")
        return

    name = path.name.lower()
    platform_hint = artifact_platform_detail(path)
    if platform_hint in {"windows", "macos", "x86_64"}:
        add_refusal(ctx, f"{label} artifact is incompatible with Linux ARM64: {path.name}")
        return
    if path.suffix == ".whl":
        if "cp312" not in name:
            add_refusal(ctx, f"{label} wheel must target cp312 for this workflow: {path.name}")
        if platform_hint not in {"aarch64", "unknown"}:
            add_refusal(ctx, f"{label} wheel does not look like Linux ARM64/aarch64: {path.name}")
    if requires_torch_runtime_hint:
        torch_suffix = ""
        version = str(ctx.torch_info.get("version") or "")
        cuda = str(ctx.torch_info.get("cuda") or "")
        if cuda:
            torch_suffix = f"cu{cuda.replace('.', '')}"
        elif "+" in version:
            torch_suffix = version.rsplit("+", 1)[-1]
        for incompatible in EXPECTED_TORCH_CUDA_SUFFIXES - ({torch_suffix} if torch_suffix else set()):
            if incompatible and incompatible in name:
                add_refusal(
                    ctx,
                    f"{label} artifact runtime tag appears incompatible with target torch ({torch_suffix or 'unknown'}): {path.name}",
                )


def stage_temp_dir(ctx: PrepContext, stage_name: str) -> Path:
    return ctx.temp_root / stage_name


def plan_cleanup(ctx: PrepContext, stage_name: str) -> None:
    temp_dir = stage_temp_dir(ctx, stage_name)
    ctx.cleanup_notes.append(f"Remove script-owned temp dir: rm -rf {shlex.quote(str(temp_dir))}")


def plan_common_bootstrap(ctx: PrepContext, stage_name: str) -> List[CommandPlan]:
    if ctx.build_helpers_bootstrap_planned:
        return []
    ctx.build_helpers_bootstrap_planned = True
    return [
        CommandPlan(
            label=f"install build helpers before {stage_name}",
            argv=[str(ctx.venv_python), "-m", "pip", "install", "--upgrade", *BUILD_HELPERS],
        )
    ]


def runtime_library_paths(ctx: PrepContext) -> List[str]:
    paths: List[str] = []
    if ctx.torch_lib_path and ctx.torch_lib_path.exists():
        paths.append(str(ctx.torch_lib_path))
    if ctx.cuda_home:
        cuda_lib64 = Path(ctx.cuda_home).expanduser() / "lib64"
        if cuda_lib64.exists():
            paths.append(str(cuda_lib64))
    return merge_library_path_entries(paths)


def has_required_runpath_entries(ctx: PrepContext) -> bool:
    return bool(ctx.torch_lib_path and ctx.torch_lib_path.exists()) and bool(
        ctx.cuda_home and (Path(ctx.cuda_home).expanduser() / "lib64").exists()
    )


def runtime_env(ctx: PrepContext, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(base or {})
    if ctx.cuda_home:
        env["CUDA_HOME"] = ctx.cuda_home
        env["CUDA_PATH"] = ctx.cuda_home
    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    paths = merge_library_path_entries(runtime_library_paths(ctx), current_ld.split(os.pathsep) if current_ld else [])
    if paths:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(paths)
    return env


def runtime_export_example(ctx: PrepContext) -> Optional[str]:
    runtime_paths = runtime_library_paths(ctx)
    if not runtime_paths:
        return None
    prefix = os.pathsep.join(runtime_paths)
    return f'export LD_LIBRARY_PATH="{prefix}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"'


def probe_pythonpath_env(ctx: PrepContext) -> Dict[str, str]:
    env = {}
    if ctx.source_layout is not None:
        current = os.environ.get("PYTHONPATH")
        pythonpath_entry = str(ctx.source_layout.pythonpath_entry)
        if current:
            pythonpath_entry = pythonpath_entry + os.pathsep + current
        env["PYTHONPATH"] = pythonpath_entry
    return runtime_env(ctx, env)


def import_probe_plan(ctx: PrepContext, module_name: str, without_ld_library_path: bool = False) -> CommandPlan:
    env = probe_pythonpath_env(ctx)
    unset_env: List[str] = []
    if without_ld_library_path:
        env.pop("LD_LIBRARY_PATH", None)
        unset_env.append("LD_LIBRARY_PATH")
    return CommandPlan(
        label=f"probe import {module_name}",
        argv=[str(ctx.venv_python), "-c", f"import {module_name}"],
        env=env,
        unset_env=unset_env,
    )


def final_probe_plan(ctx: PrepContext) -> CommandPlan:
    code = "import xatlas, mesh_processor, custom_rasterizer; from hy3dgen.texgen import Hunyuan3DPaintPipeline"
    env = probe_pythonpath_env(ctx)
    env.pop("LD_LIBRARY_PATH", None)
    return CommandPlan(
        label="final texgen probe imports",
        argv=[str(ctx.venv_python), "-c", code],
        env=env,
        unset_env=["LD_LIBRARY_PATH"],
    )


def selected_arch_list(ctx: PrepContext) -> Optional[str]:
    arch_list = ctx.arch_list or ctx.detected_sm
    if arch_list == "12.1":
        normalized = "12.0+PTX"
        ctx.warnings.append(
            "TORCH_CUDA_ARCH_LIST=12.1 is rejected by the target Torch 2.7.0+cu128 toolchain; using 12.0+PTX instead."
        )
        return normalized
    return arch_list


def build_stage_plans(ctx: PrepContext) -> List[Tuple[str, List[CommandPlan]]]:
    plans: List[Tuple[str, List[CommandPlan]]] = []
    if ctx.stage == "inspect":
        return plans
    if ctx.stage in {"xatlas", "all"}:
        commands: List[CommandPlan] = []
        if ctx.xatlas_artifact:
            commands.append(
                CommandPlan(
                    label="install xatlas artifact",
                    argv=[str(ctx.venv_python), "-m", "pip", "install", "--no-deps", str(ctx.xatlas_artifact)],
                )
            )
        else:
            commands.extend(plan_common_bootstrap(ctx, "xatlas"))
            commands.append(
                CommandPlan(
                    label="install xatlas from source/sdist",
                    argv=[str(ctx.venv_python), "-m", "pip", "install", "--no-build-isolation", "xatlas"],
                )
            )
        commands.append(import_probe_plan(ctx, "xatlas"))
        plans.append(("xatlas", commands))

    if ctx.stage in {"mesh_processor", "all"} and ctx.source_layout is not None:
        commands = []
        if ctx.mesh_processor_artifact:
            commands.append(
                CommandPlan(
                    label="install mesh_processor artifact",
                    argv=[str(ctx.venv_python), "-m", "pip", "install", "--no-deps", str(ctx.mesh_processor_artifact)],
                )
            )
        else:
            commands.extend(plan_common_bootstrap(ctx, "mesh_processor"))
            commands.append(
                CommandPlan(
                    label="install mesh_processor from differentiable_renderer",
                    argv=[
                        str(ctx.venv_python),
                        "-m",
                        "pip",
                        "install",
                        "--no-build-isolation",
                        str(ctx.source_layout.mesh_processor_dir),
                    ],
                )
            )
        commands.append(import_probe_plan(ctx, "mesh_processor"))
        plans.append(("mesh_processor", commands))

    if ctx.stage in {"custom_rasterizer", "all"} and ctx.source_layout is not None:
        commands = []
        arch_list = selected_arch_list(ctx)
        env = runtime_env(ctx, {"TORCH_CUDA_ARCH_LIST": arch_list} if arch_list else {})
        if ctx.custom_rasterizer_artifact:
            commands.append(
                CommandPlan(
                    label="install custom_rasterizer artifact",
                    argv=[str(ctx.venv_python), "-m", "pip", "install", "--no-deps", str(ctx.custom_rasterizer_artifact)],
                    env=env,
                )
            )
        else:
            commands.extend(plan_common_bootstrap(ctx, "custom_rasterizer"))
            commands.append(
                CommandPlan(
                    label="install custom_rasterizer from source",
                    argv=[
                        str(ctx.venv_python),
                        "-m",
                        "pip",
                        "install",
                        "--no-build-isolation",
                        str(ctx.source_layout.custom_rasterizer_dir),
                    ],
                    env=env,
                )
            )
        runpath = custom_rasterizer_runpath(ctx) or "<target torch/lib>:<cuda lib64>"
        search_roots = [str(path) for path in ctx.site_packages] or ["<target venv site-packages>"]
        commands.append(
            CommandPlan(
                label="patch custom_rasterizer RUNPATH",
                argv=[
                    str(ctx.patchelf_path or "patchelf"),
                    "--set-rpath",
                    runpath,
                    "custom_rasterizer_kernel*.so under " + ", ".join(search_roots),
                ],
                action="patch_custom_rasterizer_runpath",
            )
        )
        commands.append(import_probe_plan(ctx, "custom_rasterizer", without_ld_library_path=True))
        plans.append(("custom_rasterizer", commands))

    if ctx.stage in {"probe", "all"}:
        plans.append(("probe", [final_probe_plan(ctx)]))
    return plans


def record_shared_notes(ctx: PrepContext) -> None:
    ctx.rollback_notes = [
        f"Targeted rollback: {ctx.venv_pip} uninstall xatlas mesh_processor custom_rasterizer",
        f"Full rollback: rm -rf {shlex.quote(str(ctx.venv_path))} and let Modly recreate the extension venv",
        "Re-run this script with --dry-run after rollback before trying again.",
    ]
    ctx.cleanup_notes.append(f"Script-owned temp root: {ctx.temp_root}")
    if ctx.clean:
        ctx.cleanup_notes.append(f"--clean only removes script-owned temp dirs under {ctx.temp_root}")


def execute_command(plan: CommandPlan) -> None:
    if plan.action != "command":
        raise RuntimeError(f"Unsupported direct command action: {plan.action}")
    merged_env = os.environ.copy()
    for key in plan.unset_env:
        merged_env.pop(key, None)
    merged_env.update(plan.env)
    subprocess.run(plan.argv, cwd=str(plan.cwd) if plan.cwd else None, env=merged_env, check=True)


def execute_plan(ctx: PrepContext, plan: CommandPlan) -> None:
    if plan.action == "command":
        execute_command(plan)
        return
    if plan.action == "patch_custom_rasterizer_runpath":
        patch_custom_rasterizer_runpath(ctx)
        return
    raise RuntimeError(f"Unsupported plan action: {plan.action}")


def maybe_clean(ctx: PrepContext) -> None:
    if not ctx.clean:
        return
    if ctx.dry_run:
        return
    if ctx.temp_root.exists():
        shutil.rmtree(ctx.temp_root)


def print_report(ctx: PrepContext) -> None:
    print("=== Linux ARM64 Texgen Runtime Prep ===")
    print(f"repo_root: {ctx.repo_root}")
    print(f"target_venv: {ctx.venv_path}")
    print(f"stage: {ctx.stage}")
    print(f"dry_run: {ctx.dry_run}")
    if ctx.source_layout:
        print(f"source_root: {ctx.source_layout.root}")
    else:
        print("source_root: <not resolved>")
    print("")
    print("Checks:")
    for item in ctx.checks:
        marker = "OK" if item["ok"] else "FAIL"
        print(f"- [{marker}] {item['name']}: {item['detail']}")
    print("")
    if ctx.warnings:
        print("Warnings:")
        for warning in ctx.warnings:
            print(f"- {warning}")
        print("")
    print("Runtime linker guidance:")
    runtime_paths = runtime_library_paths(ctx)
    if runtime_paths:
        print("- custom_rasterizer RUNPATH target entries:")
        for path in runtime_paths:
            print(f"  - {path}")
        print("- The custom_rasterizer stage patches installed custom_rasterizer_kernel*.so files with these entries using patchelf.")
        print("- LD_LIBRARY_PATH should only be needed as a diagnostic fallback if RUNPATH patching cannot be completed.")
    else:
        print("- No additional runtime linker paths were detected from the target venv/CUDA_HOME.")
        print("- custom_rasterizer RUNPATH patching requires the target venv torch/lib and CUDA lib64 paths.")
    if ctx.patchelf_path:
        print(f"- patchelf: {ctx.patchelf_path}")
    else:
        print("- patchelf: <not found in target venv bin/ or PATH>")
    if ctx.custom_rasterizer_kernels:
        print("- Detected custom_rasterizer kernels:")
        for path in ctx.custom_rasterizer_kernels:
            print(f"  - {path}")
    else:
        print("- Detected custom_rasterizer kernels: <none yet; will search target site-packages after install>")
    export_example = runtime_export_example(ctx)
    if export_example:
        print(f"- Diagnostic fallback export: {export_example}")
    print("")
    print("Planned commands:")
    if not ctx.planned_commands:
        print("- <none>")
    for plan in ctx.planned_commands:
        cwd_prefix = f"(cwd={plan.cwd}) " if plan.cwd else ""
        print(f"- {plan.label}: {cwd_prefix}{command_prefix(plan)}{quoted(plan.argv)}")
    print("")
    print("Rollback guidance:")
    for note in ctx.rollback_notes:
        print(f"- {note}")
    print("")
    print("Cleanup guidance:")
    for note in ctx.cleanup_notes:
        print(f"- {note}")
    print("")
    if ctx.refusals:
        print("Refusals:")
        for refusal in ctx.refusals:
            print(f"- {refusal}")
    else:
        print("Refusals: none")


def build_context(args: argparse.Namespace) -> PrepContext:
    venv_path = Path(args.venv).expanduser().resolve()
    venv_python, venv_pip = ensure_venv_paths(venv_path)
    ctx = PrepContext(
        repo_root=REPO_ROOT,
        venv_path=venv_path,
        venv_python=venv_python,
        venv_pip=venv_pip,
        temp_root=SCRIPT_TEMP_ROOT,
        stage=args.stage,
        dry_run=bool(args.dry_run),
        clean=bool(args.clean),
        arch_list=args.arch_list,
        source_layout=resolve_source_layout(args.source_root),
        xatlas_artifact=Path(args.xatlas_wheel).expanduser().resolve() if args.xatlas_wheel else None,
        mesh_processor_artifact=Path(args.mesh_processor_wheel).expanduser().resolve() if args.mesh_processor_wheel else None,
        custom_rasterizer_artifact=Path(args.custom_rasterizer_wheel).expanduser().resolve() if args.custom_rasterizer_wheel else None,
    )
    return ctx


def main() -> int:
    args = parse_args()
    ctx = build_context(args)
    inspect_environment(ctx)
    require_mutation_inputs(ctx)
    validate_artifact(ctx, "xatlas", ctx.xatlas_artifact, requires_torch_runtime_hint=False)
    validate_artifact(ctx, "mesh_processor", ctx.mesh_processor_artifact, requires_torch_runtime_hint=True)
    validate_artifact(ctx, "custom_rasterizer", ctx.custom_rasterizer_artifact, requires_torch_runtime_hint=True)
    record_shared_notes(ctx)

    stage_plans = build_stage_plans(ctx)
    for stage_name, commands in stage_plans:
        plan_cleanup(ctx, stage_name)
        ctx.planned_commands.extend(commands)

    print_report(ctx)
    if ctx.refusals:
        return 1
    if ctx.dry_run or ctx.stage == "inspect":
        return 0

    maybe_clean(ctx)
    ctx.temp_root.mkdir(parents=True, exist_ok=True)

    try:
        for _, commands in stage_plans:
            for plan in commands:
                print(f"[run] {plan.label}")
                execute_plan(ctx, plan)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: stage command failed with exit code {exc.returncode}: {quoted(exc.cmd)}", file=sys.stderr)
        return exc.returncode or 1
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Runtime preparation stages completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
