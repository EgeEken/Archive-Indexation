from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


class FrontendTests(unittest.TestCase):
    def test_semantic_search_and_similar_ui_hooks_are_present(self) -> None:
        root = Path(__file__).parents[1] / "src" / "archive_index" / "web"
        html = (root / "index.html").read_text(encoding="utf-8")
        javascript = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "app.css").read_text(encoding="utf-8")
        self.assertNotIn('id="search-mode"', html)
        self.assertIn('id="setup-semantic-search"', html)
        self.assertIn("openclip-b16-datacomp-xl", html)
        self.assertIn("siglip2-base-patch16-224", html)
        self.assertIn("/api/browser?", javascript)
        self.assertIn("Find similar", html)
        self.assertIn("similarity-chip", javascript)
        self.assertIn("similarity-chip", css)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_review_buttons_and_card_contract(self):
        root = Path(__file__).parents[1] / "src" / "archive_index" / "web"
        source = (root / "app.js").read_text(encoding="utf-8")
        start = source.index("function selectionActionsMarkup")
        end = source.index("function bindSelectionButtons", start)
        script = "const escapeHtml=String;" + source[start:end] + "console.log(JSON.stringify(['undecided','selected','rejected'].map(user_decision=>selectionActionsMarkup({asset_id:'a',user_decision}))));"
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, check=True)
        undecided, selected, rejected = json.loads(result.stdout)
        self.assertIn(">Select</button>", undecided)
        self.assertIn(">Reject</button>", undecided)
        self.assertIn(">Selected</button>", selected)
        self.assertIn('select active', selected)
        self.assertIn(">Rejected</button>", rejected)
        self.assertIn('reject active', rejected)
        card = source[source.index("function renderCard"):source.index("async function loadHome")]
        self.assertNotIn('$("sort-by")', card)
        self.assertIn("Capture time unavailable", card)
        self.assertIn('aspect-ratio: 1 / 1', (root / "app.css").read_text())
        self.assertIn('}, 500)', source)

    @unittest.skipUnless(shutil.which("node"), "node is required for frontend formatter tests")
    def test_capture_formatter_keeps_offsets_and_limits_fractional_seconds(self) -> None:
        source = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app.js").read_text(encoding="utf-8")
        start = source.index("function formatCapture")
        end = source.index("\n}\n\nfunction renderReviewFilters", start) + 2
        function = source[start:end]
        script = f"const formatCapture = ({function}); console.log(JSON.stringify([formatCapture('2026-09-11T12:28:43.341000+02:00', ''), formatCapture('2026-09-11T12:28:43.000000+02:00', ''), formatCapture('2026-09-11T12:28:43', 'exif_local_unknown')]));"
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(
            json.loads(result.stdout),
            [
                "2026-09-11 12:28:43.34+02:00",
                "2026-09-11 12:28:43+02:00",
                "2026-09-11 12:28:43 · local time; timezone unknown",
            ],
        )


class CorrectionFrontendTests(unittest.TestCase):
    def run_js(self, functions, body):
        source = (Path(__file__).parents[1] / "src/archive_index/web/app.js").read_text(encoding="utf-8")
        parts = []
        for name, following in functions:
            end = source.index("function " + following)
            if source[end-6:end] == "async ": end -= 6
            parts.append(source[source.index("function " + name):end])
        result = subprocess.run([shutil.which("node"), "--eval", "const escapeHtml=String;" + "\n".join(parts) + body],capture_output=True,text=True,check=True)
        return json.loads(result.stdout)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_folder_histories_and_pluralization(self):
        result=self.run_js([("normalizeFolders", "renderFolderTree")], """
        const state={folderPaths:['all-jpgs','all-raws'],folders:null};
        toggleFolder('all-raws',false);
        const first={paths:[...state.folders].sort(),summary:folderSummary()};
        toggleFolder(null,false);toggleFolder('all-jpgs',true);
        const second={paths:[...state.folders].sort(),summary:folderSummary()};
        toggleFolder(null,true);
        const all=folderSummary();
        state.folderPaths.push('third');state.folders=new Set(['all-jpgs','all-raws']);
        console.log(JSON.stringify({first,second,all,two:folderSummary()}));
        """)
        self.assertEqual(result["first"],result["second"])
        self.assertEqual(result["first"],{"paths":["all-jpgs"],"summary":"1 folder selected"})
        self.assertEqual(result["all"],"All folders selected")
        self.assertEqual(result["two"],"2 folders selected")

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_clear_filters_persists_baseline_without_decision_writes(self):
        result=self.run_js([("setupFilters","normalizeFolders")], """
        const nodes={};const $=id=>nodes[id] ||= {value:'old',textContent:'',addEventListener(){},classList:{toggle(){}}};
        const document={querySelectorAll:()=>[],body:{classList:{toggle(){}}}};
        const localStorage={setItem(){}};const window={scrollTo(){}};
        const state={workspace:'w',auto:'recommended',manual:'selected',layout:'vertical',folders:new Set(['raw']),semanticPending:true};
        const calls=[];const api=(path,options)=>{calls.push({path,body:JSON.parse(options.body)});return Promise.resolve({});};
        const refreshBrowser=()=>{};const bindBackdropClose=()=>{};const closeDialog=()=>{};
        setupFilters();$('clear-filters').onclick();
        setImmediate(()=>console.log(JSON.stringify({state,query:$('search').value,sort:$('sort-by').value,direction:$('direction').value,type:$('media-type').value,semantic:$('similarity-threshold').value,recommendation:$('recommendation-threshold').value,calls})));
        """)
        self.assertEqual(result["calls"],[{"path":"/api/recommendation-threshold","body":{"threshold":.7}}])
        self.assertEqual((result["query"],result["type"],result["sort"],result["direction"]),("","","capture_time","desc"))
        self.assertEqual((result["semantic"],result["recommendation"]),("0.20","0.70"))
        self.assertIsNone(result["state"]["folders"])
        self.assertEqual((result["state"]["auto"],result["state"]["manual"],result["state"]["layout"]),("all","all",""))

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_video_cleanup_pauses_and_releases_source(self):
        result=self.run_js([("stopViewerMedia","closeDialog")], """
        const calls=[];const video={pause(){calls.push('pause')},removeAttribute(name){calls.push('remove '+name)},load(){calls.push('load')}};
        const $=()=>({querySelectorAll:()=>[video]});stopViewerMedia();console.log(JSON.stringify(calls));
        """)
        self.assertEqual(result,["pause","remove src","load"])
        source=(Path(__file__).parents[1]/"src/archive_index/web/app.js").read_text(encoding="utf-8")
        render=source[source.index("function renderViewer"):source.index("function applyViewerTransform")]
        self.assertLess(render.index("stopViewerMedia()"),render.index('$("viewer-media").innerHTML = ""'))
        drawer=source[source.index("async function toggleViewerInfo"):source.index("async function loadJobs")]
        self.assertNotIn("renderViewer",drawer)
        self.assertNotIn("stopViewerMedia",drawer)
        self.assertIn('addEventListener("close", stopViewerMedia)',source)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_representation_diagnostics_only_when_present(self):
        source=(Path(__file__).parents[1]/"src/archive_index/web/app.js").read_text(encoding="utf-8")
        render=source[source.index("function renderRepresentations"):source.index("function renderQuality")]
        problems=source[source.index("function componentProblemMessage"):source.index("function cameraValue")]
        script="const escapeHtml=String,formatBytes=String;"+render+problems+"""
        const base={extension:'.jpg',filename:'a.jpg',relative_path:'jpg/a.jpg',id:'a',size_bytes:50,is_online:true,components:{metadata:{status:'complete'}}};
        const healthy=renderRepresentations({physical_files:[base]});
        const warning=renderRepresentations({physical_files:[{...base,components:{thumbnail:{status:'unsupported',error:'RAW decoder unavailable'}}}]});
        console.log(JSON.stringify({healthy,warning}));
        """
        result=json.loads(subprocess.run([shutil.which('node'),'--eval',script],check=True,capture_output=True,text=True).stdout)
        self.assertNotIn('<details',result['healthy'])
        self.assertIn('Representation status',result['warning'])
        self.assertIn('RAW decoder unavailable',result['warning'])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_measurement_labels_sparse_and_overflow_numeric_value_retained(self):
        result=self.run_js([("meterMarkup","renderTechnicalDetails")], """
        console.log(JSON.stringify({normal:meterMarkup('Focal length','89 mm',logPosition(89,10,600)),overflow:meterMarkup('Focal length','15000 mm',logPosition(15000,10,600))}));
        """)
        self.assertEqual(result['normal'].count('class="scale-label'),5)
        self.assertIn('89 mm',result['normal'])
        self.assertIn('15000 mm',result['overflow'])
        self.assertIn('scale-marker overflow',result['overflow'])
        self.assertIn('left:100%',result['overflow'])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_search_helper_states_and_error_style(self):
        result=self.run_js([("showSearchStatus","prepareSearch")], """
        const nodes={};const $=id=>nodes[id] ||= {dataset:{},textContent:''};const state={};
        const states=[['ready','Semantic search ready · OpenCLIP'],['loading','Loading OpenCLIP…'],['searching','Searching…'],['complete',''],['missing_model','OpenCLIP model not installed'],['missing_embeddings','Embeddings not indexed · Re-index required'],['failed','Search failed: checkpoint unreadable']];
        const out=states.map(([phase,message])=>{showSearchStatus({state:phase,message},{media_total:12,filename_matches:1});return {phase:$('search-status').dataset.state,text:$('search-status').textContent}});
        console.log(JSON.stringify(out));
        """)
        self.assertEqual(result[0]['phase'],'ready')
        self.assertEqual(result[1]['text'],'Loading OpenCLIP…')
        self.assertEqual(result[2]['text'],'Searching…')
        self.assertEqual(result[3]['text'],'Found 12 media · 1 filename matches')
        self.assertEqual(result[-1]['phase'],'failed')
        self.assertIn('checkpoint unreadable',result[-1]['text'])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_cached_gallery_and_group_windows_skip_fetch(self):
        source=(Path(__file__).parents[1]/"src/archive_index/web/app.js").read_text(encoding="utf-8")
        gallery=source[source.index('async function loadAssets'):source.index('function bindGalleryCards')]
        groups=source[source.index('async function loadGroups'):source.index('function renderGroupPager')]
        script=gallery+groups+"""
        const state={assetRequest:0,groupRequest:0,groupPage:1,renderKeys:{}};
        const window={scrollY:0,innerHeight:720};
        const $=()=>({clientWidth:900,getBoundingClientRect:()=>({top:100})});
        const filterParams=()=>new URLSearchParams({q:'test'});
        let requests=0;const browserData=()=>{requests++;throw new Error('unexpected fetch')};
        state.renderKeys.gallery='q=test&offset=0&limit=28|4';
        state.renderKeys.groups='q=test&view=groups&offset=0&limit=10';
        Promise.all([loadAssets(),loadGroups()]).then(()=>console.log(JSON.stringify(requests)));
        """
        result=subprocess.run([shutil.which('node'),'--eval',script],check=True,capture_output=True,text=True)
        self.assertEqual(json.loads(result.stdout),0)
