from pathlib import Path
import json
import shutil
import subprocess
import unittest


class Phase10AFrontendSecondPassTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parents[1] / "src" / "archive_index" / "web"
        self.html = (self.root / "index.html").read_text(encoding="utf-8")
        self.management = (self.root / "app-file-management.js").read_text(encoding="utf-8")
        self.details = (self.root / "app-details.js").read_text(encoding="utf-8")
        self.file_management_backend = (self.root.parent / "file_management.py").read_text(encoding="utf-8")

    def test_file_management_is_header_action_with_structured_sections(self):
        self.assertLess(self.html.index('id="file-management-button"'), self.html.index('id="configure-workspace"'))
        self.assertLess(self.html.index('id="configure-workspace"'), self.html.index('id="index"'))
        self.assertNotIn("Plan changes to the files in this archive.", self.html)
        self.assertNotIn("Settings JSON", self.html)
        self.assertNotIn("Rules JSON", self.html)
        self.assertNotIn('id="file-management-output"', self.html)
        for marker in ("data-file-management-tab=\"rules\"", "data-file-management-tab=\"profiles\"", "data-file-management-tab=\"plan\"", "Analyze plan", "Custom Ruleset"):
            self.assertIn(marker, self.html + self.management)

    def test_final_rule_editor_sentence_and_layout_contract(self):
        maintenance = (self.root / "app-maintenance.js").read_text(encoding="utf-8")
        self.assertNotIn('data-rule-field="enabled"', self.management)
        self.assertNotIn("All representations", self.management)
        self.assertIn('For <select data-rule-field="representation">', self.management)
        self.assertIn('representations of <select data-rule-field="asset">', self.management)
        self.assertIn('assets → <select data-rule-field="operation">', self.management)
        self.assertIn("Rule ${index + 1}", self.management)
        self.assertIn("${label} · ${elapsed} / ${projected}", maintenance)
        self.assertNotIn("elapsed ${elapsed}", maintenance)
        self.assertNotIn("projected total", maintenance)

    def test_rules_use_semantic_controls_and_phase10b_executor(self):
        for marker in ("representation_class", "selection_state", "delete", "source_disposition", "destination_status", "executor"):
            self.assertIn(marker, self.management + self.file_management_backend)
        self.assertIn('"available": True', self.file_management_backend)
        self.assertIn("Phase 10C", self.file_management_backend)

    def test_representation_eye_and_actual_comparison_controls_exist(self):
        for marker in ("data-representation-view", "View representation", "comparison-preview", "Side by side", "Slider", "PSNR", "SharedImageCamera"):
            self.assertIn(marker, self.details + (self.root.parent / "api" / "comparison.py").read_text(encoding="utf-8"))
        self.assertNotIn("bindComparisonTransform", self.details)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_shared_camera_uses_each_side_pane_and_pointer_anchor(self):
        source = (self.root / "app-shared.js").read_text(encoding="utf-8")
        camera_source = source[source.index("class SharedImageCamera"):source.index("globalThis.SharedImageCamera")]
        script = "const MAX_VIEWER_ZOOM=40;\n" + camera_source + r'''
        const listeners = new WeakMap();
        function node(left, width, height, parent) {
          const value={style:{},offsetWidth:width,offsetHeight:height,parent,
            getBoundingClientRect:()=>{
              const match=value.style.transform?.match(/translate3d\(([-\d.]+)px, ([-\d.]+)px, 0\) scale\(([-\d.]+)\)/);
              if(!match || value===viewport) return {left,top:0,right:left+width,bottom:height,width,height};
              const x=Number(match[1]),y=Number(match[2]),z=Number(match[3]);
              const w=width*z,h=height*z,cx=left+width/2+x,cy=height/2+y;
              return {left:cx-w/2,top:cy-h/2,right:cx+w/2,bottom:cy+h/2,width:w,height:h};
            },
            addEventListener(name,fn){listeners.set(value,{...(listeners.get(value)||{}),[name]:fn})},
            removeEventListener(name){const current=listeners.get(value)||{}; delete current[name]; listeners.set(value,current)}, closest(selector){return selector==='img'?value:null},
            contains(other){return other===value}, setPointerCapture(){}, hasPointerCapture(){return false}, releasePointerCapture(){}};
          return value;
        }
        const viewport=node(0,1000,800); const left=node(0,500,800,viewport); const right=node(500,500,800,viewport);
        const camera=new SharedImageCamera({viewport,getFrames:()=>[{image:left,frame:left},{image:right,frame:right}]});
        camera.wheel({target:left,clientX:10,clientY:400,deltaY:-1,preventDefault(){}});
        const first={zoom:camera.zoom,panX:camera.panX,transform:left.style.transform,rightTransform:right.style.transform};
        camera.panX=1000; camera.panY=1000; camera.apply();
        const bounded={panX:camera.panX,panY:camera.panY,same:left.style.transform===right.style.transform};
        camera.destroy(); console.log(JSON.stringify({first,bounded,listenerCount:Object.keys(listeners.get(viewport)||{}).length}));
        '''
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, check=True)
        value = json.loads(result.stdout)
        self.assertAlmostEqual(value["first"]["zoom"], 1.2)
        self.assertGreater(value["first"]["panX"], 0)
        self.assertEqual(value["first"]["transform"], value["first"]["rightTransform"])
        self.assertLessEqual(abs(value["bounded"]["panX"]), 470)
        self.assertLessEqual(abs(value["bounded"]["panY"]), 80)
        self.assertTrue(value["bounded"]["same"])
        self.assertEqual(value["listenerCount"], 0)

    def test_comparison_and_raw_interaction_contract(self):
        css = (self.root / "app.css").read_text(encoding="utf-8")
        for marker in ("comparison-slider-base", "clipPath", "pixelated", "data-raw-exposure", "data-raw-exposure-value", "raw-development-preview", "data-comparison-metrics", "max_pixel_mse", "difference-legend", "comparisonDataCache", "differenceDataCache", "comparison-difference", "comparisonDisplayError", "comparison-difference"):
            self.assertIn(marker, self.details + css)
        self.assertNotIn("data-comparison-target", self.details)
        self.assertNotIn("PSNR", self.details)
        self.assertNotIn("Normalized pixel error", self.details)
        self.assertNotIn("difference-legend-title", self.details + css)
        self.assertNotIn("wipe.style.width", self.details)
        self.assertIn("dialog._comparisonDataKey", self.details)
        self.assertIn("Reference", self.details)
        self.assertIn('min="-5" max="5"', self.details)
        self.assertIn("openOfflineRepresentation", self.details)
        self.assertIn("byte_identical", self.details)
        self.assertIn('aria-label="Reset ${name', self.details)
        self.assertIn("raw-reset-all", self.details + css)
        self.assertIn("getBoundingClientRect", (self.root / "app-shared.js").read_text(encoding="utf-8"))
        for marker in ("Exact duplicate of preferred representation", "Pixel-identical to preferred representation", "Same size", "Reference ", "comparisonFactor"):
            self.assertIn(marker, self.details)
        self.assertNotIn("1× smaller", self.details)
        self.assertIn("Recreate source folders inside destination", (self.root / "app-file-management.js").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_comparison_mse_color_clamps_above_highest_stop(self):
        start = self.details.index("function comparisonMseColor")
        end = self.details.index("function disposeRepresentationDialog", start)
        script = self.details[start:end] + "\nconsole.log(JSON.stringify([comparisonMseColor(10), comparisonMseColor(101)]));\n"
        result = subprocess.run([shutil.which("node"), "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True)
        low, high = json.loads(result.stdout)
        self.assertNotEqual(low, high)
        self.assertEqual(high, "rgb(232,110,110)")

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_comparison_headers_round_mse_and_hide_pixel_maximum(self):
        start = self.details.index("function comparisonMseColor")
        end = self.details.index("function comparisonLabel", start)
        script = "function escapeHtml(value) { return String(value); }\nfunction formatBytes(value) { return `${value} bytes`; }\n" + self.details[start:end] + "\nconst metrics = value => comparisonMetricsMarkup({reference_bytes: 1000, compressed_bytes: 500, compressed_percent: 50, mse: value, byte_identical: false, pixel_identical: false}, {extension: '.jxl'});\nconsole.log(JSON.stringify([metrics(6.589), metrics(9.244727), metrics(16.425328), metrics(24.008241)]));\n"
        result = subprocess.run([shutil.which("node"), "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True)
        rendered = json.loads(result.stdout)
        self.assertTrue(all("MSE " in value for value in rendered))
        self.assertIn("MSE 7", rendered[0])
        self.assertIn("MSE 9", rendered[1])
        self.assertIn("MSE 16", rendered[2])
        self.assertIn("MSE 24", rendered[3])
        self.assertTrue(all("Global MSE" not in value and "Max pixel MSE" not in value for value in rendered))

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_difference_legend_uses_one_locale_aware_formatter(self):
        start = self.details.index("const comparisonLegendNumberFormat")
        end = self.details.index("function comparisonViewMarkup", start)
        script = "function escapeHtml(value) { return String(value); }\n" + self.details[start:end] + "\nconsole.log(differenceLegendMarkup({max_pixel_mse: 1000}));\n"
        result = subprocess.run([shutil.which("node"), "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertIn('aria-label="Absolute pixel MSE scale"', result.stdout)
        self.assertEqual(result.stdout.count("difference-legend-tick"), 5)
        self.assertNotIn("comparisonNumber", self.details)

    def test_file_management_layout_uses_binary_disk_labels_and_conditional_rows(self):
        management = self.management
        self.assertIn("Current free disk space", management)
        self.assertIn("Estimated free disk space after plan", management)
        self.assertIn("Temporary space upper bound", management)
        self.assertIn("formatDiskSpace", management + (self.root / "app-setup.js").read_text(encoding="utf-8"))
        self.assertIn("rule-options-compress", management)
        self.assertIn("rule-options-copy", management)
        self.assertIn("rule-checkbox", management)
        self.assertNotIn("Keep source subfolders", management)
        self.assertNotIn("Available space", management)
        self.assertIn("storageDelta ?", management)

    def test_profile_previews_and_planner_safety_controls(self):
        for marker in ("data-profile-preview", "Preview ${escapeHtml(profile.name)} compression", "profile.is_builtin ?", "profile-preview", "compression-preview/manifest.json", "conflictPolicy", "Destination conflict", "Overwrite", "data-rule-help"):
            self.assertIn(marker, self.management + self.details + self.html)
        self.assertIn("compression-preview-v1", (self.root / "assets" / "compression-preview" / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("comparison-slider-frame", self.details)
        self.assertIn("comparison-slider-sizer", self.details)
        self.assertIn("raw-loading-inline", self.details + (self.root / "app.css").read_text(encoding="utf-8"))
        self.assertIn("replaces_source_in_place", self.file_management_backend)
        self.assertIn("_next_target", self.file_management_backend)
        self.assertIn("capability_blockers", self.file_management_backend)


if __name__ == "__main__":
    unittest.main()
