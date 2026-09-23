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
        self.assertNotIn("Settings JSON", self.html)
        self.assertNotIn("Rules JSON", self.html)
        self.assertNotIn('id="file-management-output"', self.html)
        for marker in ("data-file-management-tab=\"rules\"", "data-file-management-tab=\"profiles\"", "data-file-management-tab=\"plan\"", "Analyze plan", "Custom Ruleset"):
            self.assertIn(marker, self.html + self.management)

    def test_rules_use_semantic_controls_and_no_executor(self):
        for marker in ("representation_class", "selection_state", "delete", "source_disposition", "destination_status", "executor"):
            self.assertIn(marker, self.management + self.file_management_backend)
        self.assertIn('"available": False', self.file_management_backend)

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
          const value={style:{},offsetWidth:1200,offsetHeight:800,parent,
            getBoundingClientRect:()=>({left,top:0,right:left+width,bottom:height,width,height}),
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
        for marker in ("comparison-slider-base", "clipPath", "pixelated", "data-raw-exposure", "raw-development-preview", "data-comparison-metrics"):
            self.assertIn(marker, self.details + css)
        self.assertNotIn("data-comparison-target", self.details)
        self.assertNotIn("PSNR", self.details)
        self.assertNotIn("wipe.style.width", self.details)
        self.assertIn("dialog._comparisonDataKey", self.details)
        self.assertIn("Reference", self.details)
        self.assertIn("Recreate source folders inside destination", (self.root / "app-file-management.js").read_text(encoding="utf-8"))

    def test_file_management_layout_uses_binary_disk_labels_and_conditional_rows(self):
        management = self.management
        self.assertIn("Current free disk space", management)
        self.assertIn("Estimated free disk space after plan", management)
        self.assertIn("formatDiskSpace", management + (self.root / "app-setup.js").read_text(encoding="utf-8"))
        self.assertIn("rule-options-compress", management)
        self.assertIn("rule-options-copy", management)
        self.assertIn("rule-checkbox", management)
        self.assertNotIn("Keep source subfolders", management)
        self.assertNotIn("Available space", management)
        self.assertIn("storageDelta ?", management)


if __name__ == "__main__":
    unittest.main()
