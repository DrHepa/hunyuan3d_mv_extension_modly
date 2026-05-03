import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SplitShapeTextureManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        cls.nodes = {node["id"]: node for node in cls.manifest["nodes"]}

    def test_manifest_declares_split_nodes_with_generate_owner(self):
        self.assertEqual({"generate-shape", "texture-mesh"}, set(self.nodes))
        self.assertEqual("Generate Shape Mesh", self.nodes["generate-shape"]["name"])
        self.assertEqual("Texture Mesh", self.nodes["texture-mesh"]["name"])
        self.assertEqual("generate", self.nodes["generate-shape"].get("weight_owner_id"))
        self.assertEqual("generate", self.nodes["texture-mesh"].get("weight_owner_id"))

    def test_shape_node_does_not_expose_texture_primary_params(self):
        params = {param["id"] for param in self.nodes["generate-shape"]["params_schema"]}
        self.assertFalse({
            "include_texture",
            "texture_model_variant",
            "texture_input_mode",
            "texture_inference_steps",
            "texture_render_size",
            "texture_texture_size",
            "texture_view_count",
            "mesh_path",
        } & params)
        self.assertIn("model_variant", params)
        self.assertIn("octree_resolution", params)

    def test_texture_node_has_required_routed_mesh_input_and_no_mesh_path_param(self):
        texture = self.nodes["texture-mesh"]
        inputs = {item["name"]: item for item in texture["inputs"]}
        self.assertEqual("mesh", inputs["mesh"]["type"])
        self.assertIs(True, inputs["mesh"]["required"])
        params = {param["id"] for param in texture["params_schema"]}
        self.assertIn("texture_model_variant", params)
        self.assertIn("texture_input_mode", params)
        self.assertNotIn("mesh_path", params)

    def test_texture_node_defaults_to_turbo_and_warns_standard_is_slow(self):
        params = {param["id"]: param for param in self.nodes["texture-mesh"]["params_schema"]}
        variant = params["texture_model_variant"]
        self.assertEqual("turbo", variant["default"])
        standard = next(option for option in variant["options"] if option["value"] == "standard")
        slow_copy = "%s %s" % (standard.get("label", ""), variant.get("tooltip", ""))
        self.assertIn("High quality", slow_copy)
        self.assertIn("very slow", slow_copy)

    def test_texture_quality_controls_are_texture_only_with_safe_defaults(self):
        texture_params = {param["id"]: param for param in self.nodes["texture-mesh"]["params_schema"]}
        shape_params = {param["id"] for param in self.nodes["generate-shape"]["params_schema"]}
        expected_defaults = {
            "texture_inference_steps": 30,
            "texture_render_size": 2048,
            "texture_texture_size": 2048,
            "texture_view_count": 6,
        }
        for param_id, default in expected_defaults.items():
            self.assertIn(param_id, texture_params)
            self.assertNotIn(param_id, shape_params)
            self.assertEqual(default, texture_params[param_id]["default"])

        self.assertIn(512, {option["value"] for option in texture_params["texture_render_size"]["options"]})
        self.assertIn(1024, {option["value"] for option in texture_params["texture_texture_size"]["options"]})
        self.assertIn(4, {option["value"] for option in texture_params["texture_view_count"]["options"]})


class SplitShapeTextureGeneratorStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "generator.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_generator_declares_split_dispatch_and_mesh_validation_helpers(self):
        method_names = {
            node.name
            for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("_active_node_id", method_names)
        self.assertIn("_generate_shape", method_names)
        self.assertIn("_generate_texture", method_names)
        self.assertIn("_resolve_mesh_path", method_names)
        self.assertIn("_load_mesh", method_names)
        self.assertIn("_apply_texture_controls", method_names)
        self.assertIn("_apply_texture_inference_steps", method_names)

    def test_generator_preserves_texture_compatibility_patches(self):
        self.assertIn("retrieve_timesteps as _diffusers_retrieve_timesteps", self.source)
        self.assertIn("inspect.signature(method)", self.source)
        self.assertIn("Deprecated include_texture=true", self.source)

    def test_generator_parses_and_applies_texture_controls_without_upstream_rewrite(self):
        self.assertIn("texture_inference_steps", self.source)
        self.assertIn("getattr(paint_pipeline, \"config\", None)", self.source)
        self.assertIn("render_size", self.source)
        self.assertIn("texture_size", self.source)
        self.assertIn("Multiview_Diffusion_Net", self.source)
        self.assertIn("num_inference_steps=getattr", self.source)
        self.assertNotIn("mesh decimation", self.source.lower())

    def test_texgen_failure_message_is_texture_specific(self):
        self.assertIn("Texgen execution failed during texture generation", self.source)
        self.assertNotIn("Texgen execution failed after shape generation", self.source)


if __name__ == "__main__":
    unittest.main()
