"""
Hunyuan3D-2mv - Modly extension generator.

Pipeline:
  1. Preprocess the uploaded front image and any optional side views.
  2. Run Hunyuan3DDiTFlowMatchingPipeline with front/left/back/right inputs.
  3. Export a GLB mesh to the Modly workspace.
"""
import base64
import io
import os
import platform
import sys
import threading
import time
import textwrap
import urllib.request
import uuid
import zipfile
from pathlib import Path

from PIL import Image

from services.generators.base import BaseGenerator, smooth_progress


# Redirect print to stderr so stdout stays clean for the JSON runner protocol.
_print = print


def print(*args, **kwargs):
    kwargs.setdefault("file", sys.stderr)
    _print(*args, **kwargs)


_HF_REPO_ID = "tencent/Hunyuan3D-2mv"
_TEXGEN_HF_REPO_ID = "tencent/Hunyuan3D-2"
_GITHUB_ZIP = "https://github.com/Tencent-Hunyuan/Hunyuan3D-2/archive/refs/heads/main.zip"
_HY3DGEN_PREFIX = "Hunyuan3D-2-main/hy3dgen/"
_HY3DGEN_STRIP = "Hunyuan3D-2-main/"
_TEXGEN_ROOT_DIRNAME = "Hunyuan3D-2"
_TEXGEN_DELIGHT_DIRNAME = "hunyuan3d-delight-v2-0"
_TEXGEN_PAINT_PREFIX = "hunyuan3d" + "-" + "paint-v2-0"

_SUBFOLDERS = {
    "hunyuan3d-dit-v2-mv-turbo": "hunyuan3d-dit-v2-mv-turbo",
    "hunyuan3d-dit-v2-mv-fast":  "hunyuan3d-dit-v2-mv-fast",
    "hunyuan3d-dit-v2-mv":       "hunyuan3d-dit-v2-mv",
}

_TEXTURE_VARIANTS = {
    "turbo": _TEXGEN_PAINT_PREFIX + "-turbo",
    "standard": _TEXGEN_PAINT_PREFIX,
}

_TEXTURE_INPUT_MODES = {"front", "multiview"}
_TEXTURE_INFERENCE_STEP_OPTIONS = {8, 15, 30}
_TEXTURE_SIZE_OPTIONS = {512, 1024, 2048}
_TEXTURE_VIEW_COUNT_OPTIONS = {4, 6}
_SUPPORTED_MESH_EXTENSIONS = {".glb", ".gltf", ".obj", ".ply", ".stl"}


def _safe_float(val, default):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val, default):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _safe_bool(val, default=True):
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        text = val.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
    if val is None:
        return default
    return bool(val)


def _safe_choice(val, allowed, default, label):
    if val in allowed:
        return val
    if isinstance(val, str):
        text = val.strip().lower()
        if text in allowed:
            return text
    if val not in (None, ""):
        print("[Hunyuan3D2mvGenerator] Invalid %s=%r, using default %r." % (label, val, default))
    return default


def _safe_int_choice(val, allowed, default, label):
    parsed = _safe_int(val, default)
    if parsed in allowed:
        return parsed
    if val not in (None, ""):
        print("[Hunyuan3D2mvGenerator] Invalid %s=%r, using default %r." % (label, val, default))
    return default


def _strip_data_url(value):
    if isinstance(value, str) and "," in value and value[:64].lower().startswith("data:"):
        return value.split(",", 1)[1]
    return value


class Hunyuan3D2mvGenerator(BaseGenerator):
    MODEL_ID = "hunyuan3d2mv"
    DISPLAY_NAME = "Hunyuan3D-2mv"
    VRAM_GB = 8
    MODEL_VARIANT = "hunyuan3d-dit-v2-mv-turbo"

    def is_downloaded(self):
        if self.download_check:
            return (self.model_dir / self.download_check).exists()
        marker = self.model_dir / self.MODEL_VARIANT / "model.fp16.safetensors"
        return marker.exists()

    def _is_linux_arm64(self):
        machine = platform.machine().lower()
        return platform.system() == "Linux" and machine in {"aarch64", "arm64"}

    def _hy3dgen_search_roots(self):
        roots = []
        repo_dir = Path(__file__).parent / "Hunyuan3D-2"
        roots.append(("extension-installed", None, False))
        roots.append(("extension-repo", repo_dir, False))
        roots.append(("model-cache", self.model_dir / "_hy3dgen", self._is_linux_arm64()))
        return roots

    def _download_hy3dgen(self, dest):
        dest.mkdir(parents=True, exist_ok=True)
        print("[Hunyuan3D2mvGenerator] Downloading hy3dgen source from GitHub...")
        with urllib.request.urlopen(_GITHUB_ZIP, timeout=180) as resp:
            data = resp.read()

        print("[Hunyuan3D2mvGenerator] Extracting hy3dgen...")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if not member.startswith(_HY3DGEN_PREFIX):
                    continue
                rel = member[len(_HY3DGEN_STRIP):]
                target = dest / rel
                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(member))

        print("[Hunyuan3D2mvGenerator] hy3dgen extracted to %s" % dest)

    def _ensure_hy3dgen_on_path(self):
        searched = []
        import_errors = []

        try:
            import hy3dgen  # noqa: F401

            return
        except ImportError as exc:
            import_errors.append("installed package: %s" % exc)

        for label, root, allow_download in self._hy3dgen_search_roots()[1:]:
            searched.append("%s=%s" % (label, root))

            if allow_download and not (root / "hy3dgen").exists():
                self._download_hy3dgen(root)

            if root.exists() and str(root) not in sys.path:
                sys.path.insert(0, str(root))

            try:
                import hy3dgen  # noqa: F401

                return
            except ImportError as exc:
                import_errors.append("%s: %s" % (label, exc))

        raise RuntimeError(
            "hy3dgen import failed. Searched paths: %s. Repair the extension install or remove the bad cache and retry. Details: %s"
            % ("; ".join(searched), " | ".join(import_errors))
        )

    def _init_background_remover(self):
        try:
            from hy3dgen.rembg import BackgroundRemover

            return BackgroundRemover(), None
        except Exception as exc:
            if not self._is_linux_arm64():
                raise
            print("[Hunyuan3D2mvGenerator] BackgroundRemover init failed on Linux ARM64, will retry via CPU rembg session: %s" % exc)
            return None, exc

    def load(self):
        if self._model is not None:
            return

        if not self.is_downloaded():
            self._download_weights()

        self._ensure_hy3dgen_on_path()

        import torch
        from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._dtype = torch.float16 if self._device == "cuda" else torch.float32
        self._rembg, self._rembg_init_error = self._init_background_remover()
        self._loaded_variant = None
        self._pipeline = None
        self._paint_pipeline = None
        self._paint_variant = None
        self._Pipeline = Hunyuan3DDiTFlowMatchingPipeline
        self._model = True
        print("[Hunyuan3D2mvGenerator] Ready on %s." % self._device)

    def _load_variant(self, variant):
        variant = variant if variant in _SUBFOLDERS else self.MODEL_VARIANT
        if self._loaded_variant == variant:
            return

        import torch

        print("[Hunyuan3D2mvGenerator] Loading variant: %s ..." % variant)
        if self._pipeline is not None:
            del self._pipeline
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        self._pipeline = self._Pipeline.from_pretrained(
            str(self.model_dir),
            subfolder=_SUBFOLDERS[variant],
            use_safetensors=True,
            variant="fp16",
            dtype=self._dtype,
            device=self._device,
            local_files_only=True,
        )
        self._loaded_variant = variant
        print("[Hunyuan3D2mvGenerator] Variant loaded: %s" % variant)

    def unload(self):
        self._pipeline = None
        self._loaded_variant = None
        self._paint_pipeline = None
        self._paint_variant = None
        self._model = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def generate(self, image_bytes, params, progress_cb=None, cancel_event=None):
        params = params or {}
        if self._active_node_id() == "texture-mesh":
            return self._generate_texture(image_bytes, params, progress_cb, cancel_event)
        return self._generate_shape(image_bytes, params, progress_cb, cancel_event)

    def _active_node_id(self):
        model_id = str(os.environ.get("MODEL_ID") or "").strip()
        if model_id:
            node_id = model_id.rsplit("/", 1)[-1]
            if node_id in {"generate-shape", "texture-mesh", "generate"}:
                return "generate-shape" if node_id == "generate" else node_id

        model_dir = str(os.environ.get("MODEL_DIR") or "").strip()
        if model_dir:
            basename = Path(model_dir).name
            for node_id in ("generate-shape", "texture-mesh"):
                if basename == node_id or basename.endswith("-" + node_id) or basename.endswith("_" + node_id):
                    return node_id

        if self.model_dir is not None:
            basename = self.model_dir.name
            for node_id in ("generate-shape", "texture-mesh"):
                if basename == node_id or basename.endswith("-" + node_id) or basename.endswith("_" + node_id):
                    return node_id

        return "generate-shape"

    def _preprocess_reference_images(self, image_bytes, params, remove_bg, progress_cb, cancel_event, progress_points=None):
        progress_points = progress_points or {"front": 5, "left": 10, "back": 14, "right": 18}
        self._report(progress_cb, progress_points["front"], "Preprocessing reference front view...")
        front_image = self._preprocess_bytes(image_bytes, remove_bg=remove_bg)
        self._check_cancelled(cancel_event)

        image_dict = {"front": front_image}
        for view_name in ("left", "back", "right"):
            image = self._optional_view_image(params, view_name, remove_bg)
            if image is None:
                continue
            self._report(progress_cb, progress_points.get(view_name, 10), "Preprocessing reference %s view..." % view_name)
            image_dict[view_name] = image
            self._check_cancelled(cancel_event)

        print("[Hunyuan3D2mvGenerator] reference image keys: %s" % list(image_dict.keys()))
        return image_dict

    def _generate_shape(self, image_bytes, params, progress_cb=None, cancel_event=None):
        import torch

        shape_params = self._parse_shape_params(params)
        texture_params = self._parse_texture_params(params)
        guidance_scale = _safe_float(params.get("guidance_scale"), 5.0)

        print(
            "[Hunyuan3D2mvGenerator] Shape params: variant=%s steps=%s octree=%s guidance=%.2f chunks=%s box_v=%.3f mc_level=%.4f remove_bg=%s seed=%s"
            % (
                shape_params["variant"], shape_params["steps"], shape_params["octree_res"], guidance_scale,
                shape_params["num_chunks"], shape_params["box_v"], shape_params["mc_level"], shape_params["remove_bg"], shape_params["seed"],
            )
        )

        image_dict = self._preprocess_reference_images(image_bytes, params, shape_params["remove_bg"], progress_cb, cancel_event)

        self._report(progress_cb, 22, "Loading shape model variant...")
        self._load_variant(shape_params["variant"])
        self._check_cancelled(cancel_event)

        self._report(progress_cb, 30, "Generating shape mesh...")
        stop_evt = threading.Event()
        progress_thread = None
        if progress_cb:
            progress_thread = threading.Thread(
                target=smooth_progress,
                args=(progress_cb, 30, 92, "Generating shape mesh...", stop_evt),
                daemon=True,
            )
            progress_thread.start()

        try:
            generator = torch.Generator(device=self._device).manual_seed(shape_params["seed"])
            with torch.no_grad():
                result = self._pipeline(
                    image=image_dict,
                    num_inference_steps=shape_params["steps"],
                    octree_resolution=shape_params["octree_res"],
                    guidance_scale=guidance_scale,
                    num_chunks=shape_params["num_chunks"],
                    box_v=shape_params["box_v"],
                    mc_level=shape_params["mc_level"],
                    generator=generator,
                    output_type="trimesh",
                )
                mesh = result[0]
        finally:
            stop_evt.set()
            if progress_thread:
                progress_thread.join(timeout=1.0)

        self._check_cancelled(cancel_event)
        self._report(progress_cb, 94, "Validating shape mesh...")
        self._validate_mesh(mesh, "Generated shape mesh")

        if texture_params["include_texture"]:
            print(
                "[Hunyuan3D2mvGenerator] Deprecated include_texture=true was supplied programmatically on generate-shape; prefer the texture-mesh node with a routed mesh input."
            )
            probe = self._probe_texgen(texture_params["texture_model_variant"])
            self._check_cancelled(cancel_event)
            if not probe["ok"]:
                raise RuntimeError(self._format_texgen_probe_error(probe))
            self._report(progress_cb, 95, "Texturing mesh via deprecated compatibility path...")
            texture_images = self._select_texture_images(image_dict, texture_params["texture_input_mode"])
            self._check_cancelled(cancel_event)
            paint_pipeline = self._load_paint_pipeline(probe)
            mesh = self._texture_mesh(mesh, texture_images, paint_pipeline, texture_params, probe)
            self._check_cancelled(cancel_event)
            self._validate_mesh(mesh, "Textured compatibility mesh")

        self._report(progress_cb, 98, "Exporting shape GLB...")
        self._check_cancelled(cancel_event)
        out_path = self._export_mesh(mesh)

        self._report(progress_cb, 100, "Shape mesh done")
        return str(out_path)

    def _generate_texture(self, image_bytes, params, progress_cb=None, cancel_event=None):
        texture_params = self._parse_texture_params(params)
        print(
            "[Hunyuan3D2mvGenerator] Texture params: texture_variant=%s texture_input_mode=%s remove_bg=%s texture_steps=%s render_size=%s texture_size=%s view_count=%s"
            % (
                texture_params["texture_model_variant"], texture_params["texture_input_mode"], texture_params["remove_bg"],
                texture_params["texture_inference_steps"], texture_params["texture_render_size"], texture_params["texture_texture_size"],
                texture_params["texture_view_count"],
            )
        )

        self._report(progress_cb, 3, "Validating routed mesh input...")
        mesh_path = self._resolve_mesh_path(params)
        self._check_cancelled(cancel_event)

        self._report(progress_cb, 8, "Loading routed mesh...")
        mesh = self._load_mesh(mesh_path)
        self._validate_mesh(mesh, "Routed texture source mesh")
        self._check_cancelled(cancel_event)

        image_dict = self._preprocess_reference_images(
            image_bytes,
            params,
            texture_params["remove_bg"],
            progress_cb,
            cancel_event,
            {"front": 15, "left": 20, "back": 24, "right": 28},
        )
        texture_images = self._select_texture_images(image_dict, texture_params["texture_input_mode"])
        self._check_cancelled(cancel_event)

        self._report(progress_cb, 35, "Checking texture runtime and assets...")
        probe = self._probe_texgen(texture_params["texture_model_variant"])
        self._check_cancelled(cancel_event)
        if not probe["ok"]:
            raise RuntimeError("Texture Mesh cannot start because texture runtime/assets are unavailable.\n%s" % self._format_texgen_probe_error(probe))

        paint_pipeline = self._load_paint_pipeline(probe)
        self._check_cancelled(cancel_event)

        self._report(progress_cb, 45, "Texturing routed mesh...")
        stop_evt = threading.Event()
        progress_thread = None
        if progress_cb:
            progress_thread = threading.Thread(
                target=smooth_progress,
                args=(progress_cb, 45, 94, "Texturing routed mesh...", stop_evt),
                daemon=True,
            )
            progress_thread.start()
        try:
            mesh = self._texture_mesh(mesh, texture_images, paint_pipeline, texture_params, probe)
        finally:
            stop_evt.set()
            if progress_thread:
                progress_thread.join(timeout=1.0)

        self._check_cancelled(cancel_event)
        self._validate_mesh(mesh, "Textured routed mesh")

        self._report(progress_cb, 98, "Exporting textured GLB...")
        self._check_cancelled(cancel_event)
        out_path = self._export_mesh(mesh)
        self._report(progress_cb, 100, "Texture mesh done")
        return str(out_path)

    def _parse_shape_params(self, params):
        return {
            "variant": params.get("model_variant") or self.MODEL_VARIANT,
            "steps": _safe_int(params.get("num_inference_steps"), 30),
            "octree_res": _safe_int(params.get("octree_resolution"), 380),
            "seed": _safe_int(params.get("seed"), 42),
            "num_chunks": _safe_int(params.get("num_chunks"), 8000),
            "box_v": _safe_float(params.get("box_v"), 1.01),
            "mc_level": _safe_float(params.get("mc_level"), 0.0),
            "remove_bg": _safe_bool(params.get("remove_bg"), True),
        }

    def _parse_texture_params(self, params):
        return {
            "include_texture": _safe_bool(params.get("include_texture"), False),
            "remove_bg": _safe_bool(params.get("remove_bg"), True),
            "texture_model_variant": _safe_choice(
                params.get("texture_model_variant"),
                set(_TEXTURE_VARIANTS.keys()),
                "turbo",
                "texture_model_variant",
            ),
            "texture_input_mode": _safe_choice(
                params.get("texture_input_mode"),
                _TEXTURE_INPUT_MODES,
                "front",
                "texture_input_mode",
            ),
            "texture_inference_steps": _safe_int_choice(
                params.get("texture_inference_steps"),
                _TEXTURE_INFERENCE_STEP_OPTIONS,
                30,
                "texture_inference_steps",
            ),
            "texture_render_size": _safe_int_choice(
                params.get("texture_render_size"),
                _TEXTURE_SIZE_OPTIONS,
                2048,
                "texture_render_size",
            ),
            "texture_texture_size": _safe_int_choice(
                params.get("texture_texture_size"),
                _TEXTURE_SIZE_OPTIONS,
                2048,
                "texture_texture_size",
            ),
            "texture_view_count": _safe_int_choice(
                params.get("texture_view_count"),
                _TEXTURE_VIEW_COUNT_OPTIONS,
                6,
                "texture_view_count",
            ),
        }

    def _resolve_mesh_path(self, params):
        raw_mesh_path = params.get("mesh_path")
        if raw_mesh_path in (None, ""):
            raw_mesh_path = params.get("mesh")

        if raw_mesh_path in (None, ""):
            raise RuntimeError(
                "Texture Mesh requires a routed mesh input. Connect a mesh edge to the required mesh port so Modly injects params.mesh_path."
            )
        if not isinstance(raw_mesh_path, str):
            raise RuntimeError("Texture Mesh routed mesh input must be a filesystem path string in params.mesh_path.")

        mesh_path_text = raw_mesh_path.strip()
        if not mesh_path_text:
            raise RuntimeError(
                "Texture Mesh received an empty routed mesh input. Connect a valid mesh edge so params.mesh_path is populated."
            )

        candidates = []
        raw_path = Path(mesh_path_text)
        workspace_dir = self.outputs_dir.parent
        if mesh_path_text.startswith("/workspace/"):
            candidates.append(workspace_dir / mesh_path_text[len("/workspace/"):])
        elif raw_path.is_absolute():
            candidates.append(raw_path)
        else:
            candidates.append(workspace_dir / raw_path)
            candidates.append(raw_path)

        supported = ", ".join(sorted(_SUPPORTED_MESH_EXTENSIONS))
        extension_candidates = [path for path in candidates if path.suffix.lower() in _SUPPORTED_MESH_EXTENSIONS]
        if not extension_candidates:
            raise RuntimeError(
                "Texture Mesh routed mesh input must point to a supported mesh file (%s): %s" % (supported, mesh_path_text)
            )

        searched = []
        for candidate in extension_candidates:
            searched.append(str(candidate))
            if candidate.is_file() and os.access(str(candidate), os.R_OK):
                return candidate.resolve()

        raise RuntimeError(
            "Texture Mesh routed mesh input is missing or unreadable. params.mesh_path=%r. Searched: %s"
            % (mesh_path_text, "; ".join(searched))
        )

    def _load_mesh(self, mesh_path):
        try:
            import trimesh
        except Exception as exc:
            raise RuntimeError("Texture Mesh cannot load the routed mesh because trimesh is unavailable: %s" % exc)

        try:
            mesh = trimesh.load(str(mesh_path), force="mesh")
        except Exception as exc:
            raise RuntimeError("Texture Mesh could not load routed mesh %s: %s" % (mesh_path, exc))

        if hasattr(trimesh, "Scene") and isinstance(mesh, trimesh.Scene):
            try:
                mesh = mesh.dump(concatenate=True)
            except Exception as exc:
                raise RuntimeError("Texture Mesh could not convert routed scene to a mesh from %s: %s" % (mesh_path, exc))
        return mesh

    def _texgen_root(self):
        return self.model_dir / _TEXGEN_ROOT_DIRNAME

    def _required_texture_paths(self, variant, root=None):
        root = Path(root or self._texgen_root())
        paint_folder = _TEXTURE_VARIANTS[variant]
        required = {
            "root": root,
            "paint_folder": paint_folder,
            "delight_folder": _TEXGEN_DELIGHT_DIRNAME,
            "paint_path": root / paint_folder,
            "delight_path": root / _TEXGEN_DELIGHT_DIRNAME,
        }
        if variant == "turbo":
            standard_path = root / _TEXTURE_VARIANTS["standard"]
            required["standard_text_encoder_path"] = standard_path / "text_encoder"
            required["standard_vae_path"] = standard_path / "vae"
        return required

    def _texture_component_has_weight(self, path, names):
        return path.exists() and any((path / name).exists() for name in names)

    def _missing_texture_asset_details(self, required, variant):
        missing = []
        if not required["paint_path"].exists():
            missing.append("paint=%s" % required["paint_path"])
        if not required["delight_path"].exists():
            missing.append("delight=%s" % required["delight_path"])
        if variant == "turbo":
            text_encoder_path = required["standard_text_encoder_path"]
            vae_path = required["standard_vae_path"]
            if not self._texture_component_has_weight(
                text_encoder_path,
                ("pytorch_model.bin", "model.safetensors", "tf_model.h5", "model.ckpt.index", "flax_model.msgpack"),
            ):
                missing.append("standard_text_encoder=%s" % text_encoder_path)
            if not self._texture_component_has_weight(
                vae_path,
                ("diffusion_pytorch_model.bin", "diffusion_pytorch_model.safetensors", "model.safetensors"),
            ):
                missing.append("standard_vae=%s" % vae_path)
        return missing

    def _texture_required_paths_payload(self, required):
        payload = {
            "root": str(required["root"]),
            "paint": str(required["paint_path"]),
            "delight": str(required["delight_path"]),
        }
        if "standard_text_encoder_path" in required:
            payload["standard_text_encoder"] = str(required["standard_text_encoder_path"])
        if "standard_vae_path" in required:
            payload["standard_vae"] = str(required["standard_vae_path"])
        return payload

    def _texture_download_allow_patterns(self, variant):
        required = self._required_texture_paths(variant)
        paint_folder = required["paint_folder"]
        delight_folder = required["delight_folder"]
        return [
            "%s" % paint_folder,
            "%s/*" % paint_folder,
            "%s/**/*" % paint_folder,
            "%s" % delight_folder,
            "%s/*" % delight_folder,
            "%s/**/*" % delight_folder,
            "%s/text_encoder/*" % _TEXTURE_VARIANTS["standard"],
            "%s/vae/*" % _TEXTURE_VARIANTS["standard"],
            "*.json",
            "*.yaml",
            "*.yml",
            "*.model",
            "*.txt",
            "*.py",
            "*.pth",
            "*.safetensors",
        ]

    def _ensure_texture_assets(self, variant):
        required = self._required_texture_paths(variant)
        missing_assets = self._missing_texture_asset_details(required, variant)
        if not missing_assets:
            return {
                "ok": True,
                "downloaded": False,
                "paint_root": str(required["root"]),
                "paint_path": str(required["paint_path"]),
                "delight_path": str(required["delight_path"]),
                "required_paths": self._texture_required_paths_payload(required),
            }

        required["root"].mkdir(parents=True, exist_ok=True)

        try:
            from huggingface_hub import snapshot_download
        except Exception as exc:
            return {
                "ok": False,
                "downloaded": False,
                "error": (
                    "Texture assets are missing under the extension-owned texgen root %s, and huggingface_hub is unavailable for lazy download: %s"
                    % (required["root"], exc)
                ),
                "paint_root": str(required["root"]),
                "paint_path": None,
                "delight_path": None,
                "required_paths": self._texture_required_paths_payload(required),
            }

        allow_patterns = self._texture_download_allow_patterns(variant)
        print(
            "[Hunyuan3D2mvGenerator] Missing texgen assets; downloading filtered snapshot from %s into %s ..."
            % (_TEXGEN_HF_REPO_ID, required["root"])
        )
        try:
            snapshot_download(
                repo_id=_TEXGEN_HF_REPO_ID,
                local_dir=str(required["root"]),
                allow_patterns=allow_patterns,
                ignore_patterns=[
                    "*.md",
                    "LICENSE",
                    "NOTICE",
                    ".gitattributes",
                ],
            )
        except Exception as exc:
            return {
                "ok": False,
                "downloaded": False,
                "error": (
                    "Unable to download required texture assets from %s into %s. Check network access and Hugging Face authentication/token if the repo requires it, then retry. Underlying error: %s"
                    % (_TEXGEN_HF_REPO_ID, required["root"], exc)
                ),
                "paint_root": str(required["root"]),
                "paint_path": None,
                "delight_path": None,
                "required_paths": self._texture_required_paths_payload(required),
            }

        missing_assets = self._missing_texture_asset_details(required, variant)
        if not missing_assets:
            print("[Hunyuan3D2mvGenerator] Texgen assets ready at %s." % required["root"])
            return {
                "ok": True,
                "downloaded": True,
                "paint_root": str(required["root"]),
                "paint_path": str(required["paint_path"]),
                "delight_path": str(required["delight_path"]),
                "required_paths": self._texture_required_paths_payload(required),
            }

        return {
            "ok": False,
            "downloaded": True,
            "error": (
                "Filtered texgen download completed but required assets are still incomplete under %s. Missing: %s."
                % (required["root"], "; ".join(missing_assets))
            ),
            "paint_root": str(required["root"]),
            "paint_path": None,
            "delight_path": None,
            "required_paths": self._texture_required_paths_payload(required),
        }

    def _resolve_texture_assets(self, variant):
        assets = self._ensure_texture_assets(variant)
        if assets["ok"]:
            return assets

        return {
            "ok": False,
            "paint_root": assets.get("paint_root"),
            "paint_path": assets.get("paint_path"),
            "delight_path": assets.get("delight_path"),
            "required_paths": assets["required_paths"],
            "error": assets.get("error"),
            "downloaded": assets.get("downloaded", False),
        }

    def _probe_optional_module(self, checks, module_name):
        try:
            __import__(module_name)
            checks.append({"name": module_name, "ok": True, "detail": "imported"})
        except Exception as exc:
            checks.append({"name": module_name, "ok": False, "detail": str(exc)})

    def _texgen_runtime_strategy_lines(self):
        return [
            "Runtime prep follow-up (not auto-built here):",
            "- xatlas: install a wheel compatible with this Python/platform, or build it from source if no Linux ARM64 wheel exists.",
            "- custom_rasterizer + mesh_processor: build/install the Hunyuan3D texgen extensions for THIS exact Python ABI, platform/arch, Torch, and CUDA stack.",
            "- Do NOT install x86_64 or Windows wheels on Linux ARM64.",
            "- Shape-only requests remain safe because texgen deps stay lazy behind include_texture=true.",
        ]

    def _probe_texgen(self, variant):
        checks = []
        warnings = []
        device = self._device

        try:
            import torch
            cuda_ok = bool(torch.cuda.is_available())
            device = "cuda" if cuda_ok else "cpu"
            checks.append({
                "name": "torch.cuda.is_available",
                "ok": cuda_ok,
                "detail": "resolved device=%s" % device,
            })
        except Exception as exc:
            cuda_ok = False
            checks.append({
                "name": "torch import",
                "ok": False,
                "detail": str(exc),
            })

        paint_pipeline_class = None
        try:
            from hy3dgen.texgen import Hunyuan3DPaintPipeline

            paint_pipeline_class = Hunyuan3DPaintPipeline
            checks.append({
                "name": "hy3dgen.texgen.Hunyuan3DPaintPipeline",
                "ok": True,
                "detail": "imported",
            })
        except Exception as exc:
            checks.append({
                "name": "hy3dgen.texgen.Hunyuan3DPaintPipeline",
                "ok": False,
                "detail": str(exc),
            })

        for module_name in ("xatlas", "custom_rasterizer", "mesh_processor"):
            self._probe_optional_module(checks, module_name)

        assets = self._resolve_texture_assets(variant)
        if assets["ok"]:
            checks.append({
                "name": "local texture weights",
                "ok": True,
                "detail": "paint=%s delight=%s" % (assets["paint_path"], assets["delight_path"]),
            })
        else:
            checks.append({
                "name": "local texture weights",
                "ok": False,
                "detail": assets.get("error") or "missing texture assets",
            })

        ok = all(check["ok"] for check in checks)
        return {
            "ok": ok,
            "device": device,
            "variant": variant,
            "paint_root": assets["paint_root"],
            "required_paths": assets["required_paths"],
            "checks": checks,
            "warnings": warnings,
            "paint_path": assets["paint_path"],
            "delight_path": assets["delight_path"],
            "paint_pipeline_class": paint_pipeline_class,
        }

    def _format_texgen_probe_error(self, probe):
        failed = [
            "- %s: %s" % (check["name"], check["detail"])
            for check in probe["checks"]
            if not check["ok"]
        ]
        missing_runtime_modules = [
            check["name"]
            for check in probe["checks"]
            if not check["ok"] and check["name"] in {"xatlas", "custom_rasterizer", "mesh_processor"}
        ]
        hints = [
            "Texture generation was explicitly requested but capability checks failed.",
            "This extension manages texture assets only under its own model directory.",
            "Install optional texgen runtime dependencies on a supported CUDA host and ensure the extension can access Hugging Face or that the required texture assets already exist locally.",
            "Required local folders: root=%s | paint=%s | delight=%s"
            % (
                probe["required_paths"].get("root"),
                probe["required_paths"].get("paint"),
                probe["required_paths"].get("delight"),
            ),
        ]
        if missing_runtime_modules:
            hints.append("Current runtime dependency blockers: %s" % ", ".join(missing_runtime_modules))
            hints.extend(self._texgen_runtime_strategy_lines())
        return "\n".join(hints + failed)

    def _prepare_hunyuanpaint_diffusers_compat(self):
        try:
            from hy3dgen.texgen.utils import multiview_utils
        except Exception as exc:
            raise RuntimeError("Unable to prepare HunyuanPaint diffusers compatibility patch: %s" % exc)

        if getattr(multiview_utils, "_hunyuan3d2mv_diffusers_compat", False):
            return

        source_dir = Path(multiview_utils.__file__).resolve().parents[1] / "hunyuanpaint"
        pipeline_src = source_dir / "pipeline.py"
        modules_src = source_dir / "unet" / "modules.py"
        missing = [str(path) for path in (pipeline_src, modules_src) if not path.exists()]
        if missing:
            raise RuntimeError(
                "Unable to prepare HunyuanPaint diffusers compatibility patch; missing source files: %s"
                % "; ".join(missing)
            )

        compat_dir = self._texgen_root() / "_runtime" / "hunyuanpaint_diffusers_compat"
        compat_dir.mkdir(parents=True, exist_ok=True)
        pipeline_text = pipeline_src.read_text(encoding="utf-8")
        pipeline_text = pipeline_text.replace("from .unet.modules import", "from .modules import")
        pipeline_text = pipeline_text.replace(
            "from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion import StableDiffusionPipeline, \\\n    retrieve_timesteps, rescale_noise_cfg",
            "from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion import StableDiffusionPipeline, \\\n    retrieve_timesteps as _diffusers_retrieve_timesteps, rescale_noise_cfg\n"
            "\n"
            "def retrieve_timesteps(scheduler, num_inference_steps=None, device=None, timesteps=None, sigmas=None, **kwargs):\n"
            "    import inspect\n"
            "\n"
            "    params = inspect.signature(_diffusers_retrieve_timesteps).parameters\n"
            "    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())\n"
            "    call_kwargs = {}\n"
            "    if 'num_inference_steps' in params:\n"
            "        call_kwargs['num_inference_steps'] = num_inference_steps\n"
            "    if 'device' in params:\n"
            "        call_kwargs['device'] = device\n"
            "    if timesteps is not None and ('timesteps' in params or accepts_kwargs):\n"
            "        call_kwargs['timesteps'] = timesteps\n"
            "    if sigmas is not None and ('sigmas' in params or accepts_kwargs):\n"
            "        call_kwargs['sigmas'] = sigmas\n"
            "    call_kwargs.update(kwargs)\n"
            "\n"
            "    try:\n"
            "        return _diffusers_retrieve_timesteps(scheduler, **call_kwargs)\n"
            "    except TypeError:\n"
            "        if 'sigmas' not in call_kwargs:\n"
            "            raise\n"
            "        call_kwargs.pop('sigmas', None)\n"
            "        return _diffusers_retrieve_timesteps(scheduler, **call_kwargs)",
        )
        pipeline_text = pipeline_text.replace(
            "from diffusers.callbacks import MultiPipelineCallbacks, PipelineCallback",
            "try:\n"
            "    from diffusers.callbacks import MultiPipelineCallbacks, PipelineCallback\n"
            "except Exception:\n"
            "    class PipelineCallback:\n"
            "        tensor_inputs = []\n"
            "    class MultiPipelineCallbacks:\n"
            "        tensor_inputs = []",
        )
        modules_text = modules_src.read_text(encoding="utf-8")
        modules_text = modules_text.replace(
            "        self.is_turbo = is_turbo\n\n        # multiview attn",
            "        self.is_turbo = is_turbo\n"
            "\n"
            "        if not hasattr(self.transformer, 'dim'):\n"
            "            self.transformer.dim = self.attn1.to_q.in_features\n"
            "        if not hasattr(self.transformer, 'num_attention_heads'):\n"
            "            self.transformer.num_attention_heads = self.attn1.heads\n"
            "        if not hasattr(self.transformer, 'attention_head_dim'):\n"
            "            self.transformer.attention_head_dim = self.attn1.inner_dim // self.attn1.heads\n"
            "        if not hasattr(self.transformer, 'dropout'):\n"
            "            self.transformer.dropout = self.attn1.to_out[1].p if len(self.attn1.to_out) > 1 else 0.0\n"
            "        if not hasattr(self.transformer, 'attention_bias'):\n"
            "            self.transformer.attention_bias = self.attn1.to_q.bias is not None\n"
            "\n"
            "        # multiview attn",
        )
        modules_text = modules_text.replace(
            "        unet_ckpt_path = os.path.join(pretrained_model_name_or_path, 'diffusion_pytorch_model.bin')\n"
            "        with open(config_path, 'r', encoding='utf-8') as file:\n"
            "            config = json.load(file)\n"
            "        unet = UNet2DConditionModel(**config)\n"
            "        unet = UNet2p5DConditionModel(unet)\n"
            "        unet_ckpt = torch.load(unet_ckpt_path, map_location='cpu', weights_only=True)\n",
            "        unet_ckpt_path = os.path.join(pretrained_model_name_or_path, 'diffusion_pytorch_model.bin')\n"
            "        unet_safetensors_path = os.path.join(pretrained_model_name_or_path, 'diffusion_pytorch_model.safetensors')\n"
            "        with open(config_path, 'r', encoding='utf-8') as file:\n"
            "            config = json.load(file)\n"
            "        unet = UNet2DConditionModel(**config)\n"
            "        unet = UNet2p5DConditionModel(unet)\n"
            "        if os.path.exists(unet_safetensors_path):\n"
            "            from safetensors.torch import load_file\n"
            "            unet_ckpt = load_file(unet_safetensors_path, device='cpu')\n"
            "        else:\n"
            "            unet_ckpt = torch.load(unet_ckpt_path, map_location='cpu', weights_only=True)\n",
        )
        modules_text = modules_text.replace("unet.load_state_dict(unet_ckpt, strict=True)", "unet.load_state_dict(unet_ckpt, strict=False)")
        (compat_dir / "pipeline.py").write_text(pipeline_text, encoding="utf-8")
        (compat_dir / "modules.py").write_text(modules_text, encoding="utf-8")

        original_from_pretrained = multiview_utils.DiffusionPipeline.from_pretrained
        source_dir_resolved = source_dir.resolve()

        class _CompatDiffusionPipeline:
            @staticmethod
            def from_pretrained(*args, **kwargs):
                custom_pipeline = kwargs.get("custom_pipeline")
                if custom_pipeline:
                    try:
                        if Path(str(custom_pipeline)).resolve() == source_dir_resolved:
                            kwargs["custom_pipeline"] = str(compat_dir)
                            model_path = Path(str(args[0])) if args else None
                            text_encoder_path = model_path / "text_encoder" if model_path is not None else None
                            if text_encoder_path is not None and text_encoder_path.exists():
                                has_text_encoder_weights = any(
                                    (text_encoder_path / name).exists()
                                    for name in (
                                        "pytorch_model.bin",
                                        "model.safetensors",
                                        "tf_model.h5",
                                        "model.ckpt.index",
                                        "flax_model.msgpack",
                                    )
                                )
                                if not has_text_encoder_weights:
                                    standard_path = model_path.with_name(_TEXTURE_VARIANTS["standard"])
                                    standard_text_encoder_path = standard_path / "text_encoder"
                                    if standard_text_encoder_path.exists():
                                        from transformers import CLIPTextModel

                                        kwargs.setdefault(
                                            "text_encoder",
                                            CLIPTextModel.from_pretrained(
                                                str(standard_text_encoder_path),
                                                torch_dtype=kwargs.get("torch_dtype"),
                                            ),
                                        )
                                    else:
                                        kwargs.setdefault("text_encoder", None)
                            vae_path = model_path / "vae" if model_path is not None else None
                            if vae_path is not None and vae_path.exists():
                                has_vae_weights = any(
                                    (vae_path / name).exists()
                                    for name in (
                                        "diffusion_pytorch_model.bin",
                                        "diffusion_pytorch_model.safetensors",
                                        "model.safetensors",
                                    )
                                )
                                if not has_vae_weights:
                                    standard_path = model_path.with_name(_TEXTURE_VARIANTS["standard"])
                                    standard_vae_path = standard_path / "vae"
                                    if standard_vae_path.exists():
                                        from diffusers import AutoencoderKL

                                        kwargs.setdefault(
                                            "vae",
                                            AutoencoderKL.from_pretrained(
                                                str(standard_vae_path),
                                                torch_dtype=kwargs.get("torch_dtype"),
                                            ),
                                        )
                    except Exception:
                        pass
                return original_from_pretrained(*args, **kwargs)

        multiview_utils.DiffusionPipeline = _CompatDiffusionPipeline
        multiview_utils._hunyuan3d2mv_diffusers_compat = True
        print("[Hunyuan3D2mvGenerator] Prepared HunyuanPaint diffusers compatibility files at %s." % compat_dir)

    def _load_paint_pipeline(self, probe):
        variant = probe["variant"]
        if self._paint_pipeline is not None and self._paint_variant == variant:
            return self._paint_pipeline

        paint_root_raw = probe.get("paint_root")
        paint_path_raw = probe.get("paint_path")
        delight_path_raw = probe.get("delight_path")
        paint_root = Path(paint_root_raw) if paint_root_raw else None
        paint_path = Path(paint_path_raw) if paint_path_raw else None
        delight_path = Path(delight_path_raw) if delight_path_raw else None
        paint_folder = _TEXTURE_VARIANTS.get(variant) or paint_path.name

        missing_paths = []
        for label, path in (("paint_root", paint_root), ("paint_path", paint_path), ("delight_path", delight_path)):
            if path is None or not path.exists():
                missing_paths.append("%s=%s" % (label, path))
        if missing_paths:
            raise RuntimeError(
                "Unable to initialize Hunyuan3DPaintPipeline because required local texture assets are missing: %s"
                % "; ".join(missing_paths)
            )

        PaintPipeline = probe.get("paint_pipeline_class")
        if PaintPipeline is None:
            from hy3dgen.texgen import Hunyuan3DPaintPipeline

            PaintPipeline = Hunyuan3DPaintPipeline

        self._prepare_hunyuanpaint_diffusers_compat()

        if hasattr(PaintPipeline, "from_pretrained"):
            try:
                pipeline = PaintPipeline.from_pretrained(str(paint_root), subfolder=paint_folder)
                self._paint_pipeline = pipeline
                self._paint_variant = variant
                print(
                    "[Hunyuan3D2mvGenerator] Loaded texgen pipeline via from_pretrained(root, subfolder=%s)."
                    % paint_folder
                )
                return pipeline
            except Exception as exc:
                raise RuntimeError(
                    "Unable to initialize Hunyuan3DPaintPipeline from local assets. Use the Hunyuan3D-2 repo root as model_path and the paint folder name as subfolder; do not pass device/local_files_only/delight_model_path to from_pretrained. Tried paint_root=%s paint_path=%s delight_path=%s. Details: %s"
                    % (paint_root, paint_path, delight_path, exc)
                )

        raise RuntimeError(
            "Unable to initialize Hunyuan3DPaintPipeline because this hy3dgen.texgen implementation does not expose from_pretrained. Tried paint_root=%s paint_path=%s delight_path=%s."
            % (paint_root, paint_path, delight_path)
        )

    def _select_texture_images(self, image_dict, mode):
        front_image = image_dict["front"]
        if mode == "front":
            return [front_image]

        ordered_views = [image_dict.get(name) for name in ("front", "left", "back", "right") if image_dict.get(name) is not None]
        missing_sides = [name for name in ("left", "back", "right") if image_dict.get(name) is None]
        if missing_sides:
            print(
                "[Hunyuan3D2mvGenerator] texture_input_mode=multiview requested but missing side views (%s); downgrading to front-only texturing."
                % ", ".join(missing_sides)
            )
            return [front_image]

        return ordered_views

    def _apply_texture_controls(self, paint_pipeline, texture_params):
        config = getattr(paint_pipeline, "config", None)
        if config is None:
            if texture_params["texture_render_size"] != 2048 or texture_params["texture_texture_size"] != 2048 or texture_params["texture_view_count"] != 6:
                raise RuntimeError(
                    "Non-default texture size/view controls were requested, but this Hunyuan3DPaintPipeline has no config object to mutate. Use default size/view controls or update the texture runtime."
                )
            print("[Hunyuan3D2mvGenerator] Hunyuan3DPaintPipeline has no config object; texture size controls cannot be applied.")
        else:
            setattr(config, "render_size", texture_params["texture_render_size"])
            setattr(config, "texture_size", texture_params["texture_texture_size"])
            self._apply_texture_view_count(config, texture_params["texture_view_count"])

        step_patch_status = self._apply_texture_inference_steps(paint_pipeline, texture_params["texture_inference_steps"])
        effective_render_size = getattr(config, "render_size", texture_params["texture_render_size"]) if config is not None else texture_params["texture_render_size"]
        effective_texture_size = getattr(config, "texture_size", texture_params["texture_texture_size"]) if config is not None else texture_params["texture_texture_size"]
        print(
            "[Hunyuan3D2mvGenerator] Effective texture controls: variant=%s mode=%s steps=%s render_size=%s texture_size=%s view_count=%s step_override=%s"
            % (
                texture_params["texture_model_variant"],
                texture_params["texture_input_mode"],
                texture_params["texture_inference_steps"],
                effective_render_size,
                effective_texture_size,
                texture_params["texture_view_count"],
                step_patch_status,
            )
        )

    def _apply_texture_view_count(self, config, view_count):
        if view_count == 6:
            return

        candidate_attrs = (
            "candidate_camera_azims",
            "candidate_camera_elevs",
            "camera_azims",
            "camera_elevs",
            "render_camera_azims",
            "render_camera_elevs",
            "view_azims",
            "view_elevs",
            "view_weights",
            "view_ids",
            "view_names",
        )
        mutated = []
        for attr in candidate_attrs:
            value = getattr(config, attr, None)
            if isinstance(value, (list, tuple)) and len(value) >= view_count:
                setattr(config, attr, list(value[:view_count]))
                mutated.append(attr)

        if not mutated:
            raise RuntimeError(
                "Texture view_count=%s was requested, but this Hunyuan3DPaintPipeline config does not expose supported mutable camera/view lists. Use view_count=6 or update the texture runtime."
                % view_count
            )
        print("[Hunyuan3D2mvGenerator] Applied texture view_count=%s to config attrs: %s" % (view_count, ", ".join(mutated)))

    def _apply_texture_inference_steps(self, paint_pipeline, steps):
        setattr(paint_pipeline, "_hunyuan3d2mv_texture_inference_steps", steps)
        try:
            from hy3dgen.texgen.utils import multiview_utils
        except Exception as exc:
            if steps != 30:
                raise RuntimeError("Unable to apply texture_inference_steps=%s because multiview_utils could not be imported: %s" % (steps, exc))
            return "default-30-no-multiview-import"

        setattr(multiview_utils, "_hunyuan3d2mv_texture_inference_steps", steps)
        net_cls = getattr(multiview_utils, "Multiview_Diffusion_Net", None)
        if net_cls is None or not hasattr(net_cls, "__call__"):
            if steps != 30:
                raise RuntimeError("Unable to apply texture_inference_steps=%s because Multiview_Diffusion_Net.__call__ was not found." % steps)
            return "default-30-no-call-patch"

        if getattr(net_cls, "_hunyuan3d2mv_steps_patch", False):
            try:
                net_cls.__call__.__globals__["_hunyuan3d2mv_texture_inference_steps"] = steps
            except Exception:
                pass
            return "patched-call"

        original_call = net_cls.__call__
        try:
            import inspect

            source = textwrap.dedent(inspect.getsource(original_call))
        except Exception as exc:
            if steps != 30:
                raise RuntimeError("Unable to apply texture_inference_steps=%s because Multiview_Diffusion_Net.__call__ source is unavailable: %s" % (steps, exc))
            return "default-30-source-unavailable"

        patched = source.replace(
            "num_inference_steps=30",
            "num_inference_steps=getattr(self, '_hunyuan3d2mv_texture_inference_steps', _hunyuan3d2mv_texture_inference_steps)",
        ).replace(
            "num_inference_steps = 30",
            "num_inference_steps = getattr(self, '_hunyuan3d2mv_texture_inference_steps', _hunyuan3d2mv_texture_inference_steps)",
        )
        if patched == source:
            if steps != 30:
                raise RuntimeError("Unable to apply texture_inference_steps=%s because the expected upstream hardcoded 30 was not found." % steps)
            return "default-30-hardcode-not-found"

        namespace = original_call.__globals__
        namespace["_hunyuan3d2mv_texture_inference_steps"] = steps
        try:
            exec(compile(patched, getattr(original_call, "__code__", None).co_filename if hasattr(original_call, "__code__") else "<hunyuan3d2mv_texture_steps_patch>", "exec"), namespace)
        except Exception as exc:
            if steps != 30:
                raise RuntimeError("Unable to compile texture inference step override for steps=%s: %s" % (steps, exc))
            return "default-30-patch-compile-failed"

        patched_call = namespace.get("__call__")
        if patched_call is None:
            if steps != 30:
                raise RuntimeError("Unable to apply texture_inference_steps=%s because patched __call__ was not produced." % steps)
            return "default-30-patched-call-missing"

        net_cls.__call__ = patched_call
        net_cls._hunyuan3d2mv_original_call = original_call
        net_cls._hunyuan3d2mv_steps_patch = True
        print("[Hunyuan3D2mvGenerator] Patched Multiview_Diffusion_Net.__call__ to honor texture_inference_steps.")
        return "patched-call"

    def _texture_mesh(self, mesh, texture_images, paint_pipeline, texture_params, probe):
        import inspect

        self._apply_texture_controls(paint_pipeline, texture_params)

        call_candidates = []
        if hasattr(paint_pipeline, "paint"):
            call_candidates.append(("paint", paint_pipeline.paint))
        call_candidates.append(("__call__", paint_pipeline))

        kwargs_candidates = [
            {"images": texture_images},
            {"image_list": texture_images},
            {"image": texture_images[0]},
        ]

        errors = []
        for method_name, method in call_candidates:
            try:
                signature = inspect.signature(method)
                parameters = signature.parameters
                accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
            except (TypeError, ValueError):
                parameters = {}
                accepts_kwargs = True

            for kwargs in kwargs_candidates:
                candidate_kwargs = dict(kwargs)
                if not accepts_kwargs:
                    unsupported_keys = [key for key in candidate_kwargs if key not in parameters]
                    if unsupported_keys:
                        continue
                if method_name == "paint" and "image" in candidate_kwargs and len(texture_images) > 1:
                    continue
                try:
                    result = method(mesh, **candidate_kwargs)
                    textured_mesh = self._extract_textured_mesh(result)
                    print(
                        "[Hunyuan3D2mvGenerator] Textured mesh generated via %s with keys=%s for variant=%s mode=%s."
                        % (method_name, sorted(candidate_kwargs.keys()), probe["variant"], texture_params["texture_input_mode"])
                    )
                    return textured_mesh
                except TypeError as exc:
                    errors.append("%s(%s): %s" % (method_name, ",".join(sorted(candidate_kwargs.keys())), exc))
                except Exception as exc:
                    errors.append("%s(%s): %s" % (method_name, ",".join(sorted(candidate_kwargs.keys())), exc))

        raise RuntimeError("Texgen execution failed during texture generation. Details: %s" % " | ".join(errors))

    def _extract_textured_mesh(self, result):
        if result is None:
            raise RuntimeError("Texgen returned no mesh")
        if isinstance(result, (list, tuple)) and result:
            return result[0]
        if hasattr(result, "mesh") and result.mesh is not None:
            return result.mesh
        return result

    def _validate_mesh(self, mesh, label="Generated mesh"):
        if mesh is None:
            raise RuntimeError("%s is None" % label)
        if not hasattr(mesh, "vertices") or mesh.vertices is None or len(mesh.vertices) == 0:
            raise RuntimeError("%s has no vertices" % label)
        if not hasattr(mesh, "faces") or mesh.faces is None or len(mesh.faces) == 0:
            raise RuntimeError("%s has no faces" % label)

        print("[Hunyuan3D2mvGenerator] %s validated: %d vertices, %d faces" % (label, len(mesh.vertices), len(mesh.faces)))

    def _export_mesh(self, mesh):
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.outputs_dir / ("%d_%s.glb" % (int(time.time()), uuid.uuid4().hex[:8]))
        mesh.export(str(out_path))
        print("[Hunyuan3D2mvGenerator] Exported GLB to: %s" % out_path)
        return out_path

    def _optional_view_image(self, params, view_name, remove_bg):
        path_key = "%s_image_path" % view_name
        data_key = "%s_image" % view_name

        path = params.get(path_key)
        if isinstance(path, str) and path.strip() and os.path.isfile(path):
            return self._preprocess_path(path, remove_bg=remove_bg)

        raw = params.get(data_key)
        if raw in (None, ""):
            return None

        if isinstance(raw, str):
            if params.get(data_key + "_is_b64"):
                raw = base64.b64decode(_strip_data_url(raw))
            elif os.path.isfile(raw):
                return self._preprocess_path(raw, remove_bg=remove_bg)
            else:
                try:
                    raw = base64.b64decode(_strip_data_url(raw), validate=True)
                except Exception:
                    print("[Hunyuan3D2mvGenerator] Ignoring %s: not a file or base64 image." % data_key)
                    return None

        if isinstance(raw, bytearray):
            raw = bytes(raw)
        if not isinstance(raw, bytes):
            print("[Hunyuan3D2mvGenerator] Ignoring %s: unsupported value type %s." % (data_key, type(raw).__name__))
            return None

        return self._preprocess_bytes(raw, remove_bg=remove_bg)

    def _preprocess_bytes(self, image_bytes, remove_bg=True):
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        return self._remove_bg(img) if remove_bg else img

    def _preprocess_path(self, path, remove_bg=True):
        img = Image.open(path).convert("RGB")
        return self._remove_bg(img) if remove_bg else img

    def _remove_bg(self, img):
        if self._rembg is not None:
            try:
                return self._rembg(img)
            except Exception as exc:
                print("[Hunyuan3D2mvGenerator] BackgroundRemover failed, retrying fallback path: %s" % exc)

        if self._is_linux_arm64():
            try:
                import rembg

                session = rembg.new_session("u2net", providers=["CPUExecutionProvider"])
                return rembg.remove(img, session=session)
            except Exception as exc:
                init_msg = ""
                if self._rembg_init_error is not None:
                    init_msg = " (initial BackgroundRemover error: %s)" % self._rembg_init_error
                print("[Hunyuan3D2mvGenerator] Linux ARM64 rembg CPU fallback failed, using original image: %s%s" % (exc, init_msg))
                return img

        print("[Hunyuan3D2mvGenerator] Background removal unavailable, using original image.")
        return img

    def _auto_download(self):
        self._download_weights()

    def _download_weights(self):
        from huggingface_hub import snapshot_download

        repo_id = self.hf_repo or _HF_REPO_ID
        manifest_skips = list(getattr(self, "hf_skip_prefixes", []) or [])
        ignore = []
        for pattern in manifest_skips:
            ignore.append(pattern)
            if isinstance(pattern, str) and pattern.endswith("/"):
                ignore.append(pattern + "*")
        ignore += [
            "*.md",
            "*.txt",
            "LICENSE",
            "NOTICE",
            "Notice.txt",
            ".gitattributes",
        ]
        self.model_dir.mkdir(parents=True, exist_ok=True)
        print("[Hunyuan3D2mvGenerator] Downloading weights from %s ..." % repo_id)
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(self.model_dir),
            ignore_patterns=ignore,
        )
        print("[Hunyuan3D2mvGenerator] Weights downloaded.")


class Hunyuan3D2mvTurboGenerator(Hunyuan3D2mvGenerator):
    MODEL_VARIANT = "hunyuan3d-dit-v2-mv-turbo"


class Hunyuan3D2mvFastGenerator(Hunyuan3D2mvGenerator):
    MODEL_VARIANT = "hunyuan3d-dit-v2-mv-fast"


class Hunyuan3D2mvStandardGenerator(Hunyuan3D2mvGenerator):
    MODEL_VARIANT = "hunyuan3d-dit-v2-mv"
