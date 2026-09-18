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
        self.assertIn('id="setup-quality"', html)
        self.assertIn('id="setup-video-participation"', html)
        self.assertIn('id="setup-video-section"', html)
        self.assertIn("OpenCLIP ViT-B/16 DataComp XL", javascript)
        for removed in ("Provider/model:", "Workspace ETA:", "Runtime and checkpoint", "Active semantic provider"):
            self.assertNotIn(removed, javascript)
        self.assertIn("font-size:inherit", css)
        self.assertIn("height:1.15em", css)
        self.assertNotIn("siglip", html.lower())
        self.assertNotIn("siglip", javascript.lower())
        for removed in ("Index rendered images", "Index RAW files", "Index videos", "Assess RAW-only image quality", "Include videos in semantic search"):
            self.assertNotIn(removed, html)
        self.assertEqual(html.count('setup-section panel'), 4)
        self.assertIn("setup-diagnostics", html)
        self.assertNotIn("image_extensions", html)
        self.assertIn("/api/browser?", javascript)
        self.assertIn("Show similar images", html)
        self.assertIn("Show image group", html)
        self.assertIn("similarity-chip", javascript)
        self.assertIn("similarity-chip", css)
        self.assertIn("Index these folders:", html)
        self.assertNotIn("1 —", html)
        self.assertNotIn("2 —", html)
        self.assertNotIn("3 —", html)
        self.assertNotIn("4 —", html)
        self.assertNotIn("Every supported media format", html)
        self.assertNotIn("Enable semantic search", html)
        self.assertNotIn("Models are installed explicitly", html)
        self.assertNotIn("Estimated from local completed-job timings", javascript)
        self.assertEqual(html.count("Automatically assess media quality"), 1)
        self.assertEqual(html.count("Let me search by semantic content"), 1)
        self.assertEqual(html.count('id="setup-quality"'), 1)
        self.assertEqual(html.count('id="setup-semantic-search"'), 1)
        self.assertEqual(html.count('id="setup-video-participation"'), 1)
        self.assertIn("class=\"model-card", html)
        self.assertNotIn("<h2>Diagnostics</h2>", html)
        self.assertNotIn("Intentionally skipped work is not a problem", html)

    def test_filename_sort_control_is_available_before_quality(self) -> None:
        html = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "index.html").read_text(encoding="utf-8")
        sort_row = html[html.index('id="sort-buttons"'):html.index("</div>", html.index('id="sort-buttons"'))]
        self.assertIn('data-sort="filename">Filename</button>', sort_row)
        self.assertLess(sort_row.index('data-sort="capture_time"'), sort_row.index('data-sort="filename"'))
        self.assertLess(sort_row.index('data-sort="filename"'), sort_row.index('data-sort="quality"'))
        self.assertIn('id="search-sort-button" class="hidden"', sort_row)

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
        self.assertNotIn("Capture time unavailable", card)
        self.assertIn('id="setup-header-summary"', (root / "index.html").read_text(encoding="utf-8"))
        self.assertEqual((root / "index.html").read_text(encoding="utf-8").count('id="setup-index-summary"'), 1)
        self.assertIn("display_url || item.original_url", source)
        self.assertIn('aspect-ratio: 1 / 1', (root / "app.css").read_text())
        self.assertIn('}, 500)', source)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_recommended_tag_replaces_representative_tag(self):
        source=(Path(__file__).parents[1]/"src/archive_index/web/app.js").read_text(encoding="utf-8")
        start=source.index("function selectionStateMarkup")
        end=source.index("function selectionActionsMarkup",start)
        script="const escapeHtml=String;"+source[start:end]+"""
        console.log(JSON.stringify({representative:selectionStateMarkup({is_representative:true,auto_recommended:false}),recommended:selectionStateMarkup({is_representative:true,auto_recommended:true})}));
        """
        result=json.loads(subprocess.run([shutil.which("node"),"--eval",script],capture_output=True,text=True,encoding="utf-8",check=True).stdout)
        self.assertIn("Representative", result["representative"])
        self.assertNotIn("Recommended", result["representative"])
        self.assertIn("Recommended", result["recommended"])
        self.assertNotIn("Representative", result["recommended"])

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
                "2026-09-11 12:28:43.34",
                "2026-09-11 12:28:43",
                "2026-09-11 12:28:43",
            ],
        )

    @unittest.skipUnless(shutil.which("node"), "node is required for details formatter tests")
    def test_details_layout_contract_separates_standalone_and_drawer_quality(self) -> None:
        source = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app.js").read_text(encoding="utf-8")
        start = source.index("function renderDetails")
        end = source.index("function renderRepresentations", start)
        function = source[start:end]
        script = f"""
        const escapeHtml=String,formatBytes=String,formatCapture=()=>"2026-09-13 19:37:59.27";
        const renderTechnicalDetails=()=>'<section class="section"><h3>Technical details</h3></section>';
        const renderQuality=()=>'<section class="file-card"><div class="quality-summary">Overall technical quality</div></section>';
        const renderRepresentations=()=>'<section class="section"><h3>Representations</h3></section>';
        {function}
        const asset={{capture_time:'2026-09-13T19:37:59.270000+03:00',physical_files:[{{filename:'DSC08259.JPG',absolute_path:'C:\\\\Archive\\\\DSC08259.JPG',relative_path:'all-jpgs/DSC08259.JPG',thumbnail_url:'/thumb.jpg',size_bytes:7120000,width:4240,height:2832,file_created_time:'2026-09-12T10:11:12'}}]}};
        console.log(JSON.stringify({{standalone:renderDetails(asset,{{standalone:true,showThumbnail:true}}),drawer:renderDetails(asset,{{viewerPanel:true}})}}));
        """
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        output = json.loads(result.stdout)
        self.assertIn("C:\\Archive\\DSC08259.JPG", output["standalone"])
        self.assertNotIn("<dt>Path</dt>", output["standalone"])
        self.assertEqual(output["standalone"].count("Capture time"), 1)
        self.assertIn("File created", output["standalone"])
        self.assertLess(output["standalone"].index("detail-thumbnail"), output["standalone"].index("Overall technical quality"))
        self.assertIn("<dt>Path</dt>", output["drawer"])
        self.assertEqual(output["drawer"].count("Overall technical quality"), 1)
        self.assertIn("viewer-technical-quality", output["drawer"])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_details_omits_missing_capture_time(self) -> None:
        source = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app.js").read_text(encoding="utf-8")
        start = source.index("function renderDetails")
        end = source.index("function renderRepresentations", start)
        function = source[start:end]
        script = f'''
        const escapeHtml=String,formatBytes=String,formatCapture=(value)=>value ? "formatted" : "";
        const renderTechnicalDetails=()=>"";
        const renderQuality=()=>"";
        const renderRepresentations=()=>"";
        {function}
        const asset={{capture_time:null,physical_files:[{{filename:"photo.jpg",size_bytes:100}}]}};
        console.log(renderDetails(asset));
        '''
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertNotIn("<dt>Capture time</dt>", result.stdout)
        self.assertNotIn("Capture time unavailable", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_details_location_uses_coordinates_and_safe_google_maps_link(self) -> None:
        source = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app.js").read_text(encoding="utf-8")
        start = source.index("function renderDetails")
        end = source.index("function renderRepresentations", start)
        function = source[start:end]
        script = f'''
        const escapeHtml=value=>String(value).replaceAll("&","&amp;").replaceAll('"',"&quot;");
        const formatBytes=String,formatCapture=()=>'',renderTechnicalDetails=()=>'',renderQuality=()=>'',renderRepresentations=()=>'';
        {function}
        const withLocation=renderDetails({{location:{{latitude:40.987654,longitude:-73.123456}},physical_files:[{{filename:"photo.jpg",size_bytes:100}}]}});
        const withoutLocation=renderDetails({{physical_files:[{{filename:"photo.jpg",size_bytes:100}}]}});
        console.log(JSON.stringify({{withLocation,withoutLocation}}));
        '''
        result = json.loads(subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
        self.assertIn("<dt>Location</dt>", result["withLocation"])
        self.assertIn("40.987654, -73.123456", result["withLocation"])
        self.assertIn("query=40.987654%2C-73.123456", result["withLocation"])
        self.assertIn('target="_blank"', result["withLocation"])
        self.assertIn('rel="noopener noreferrer"', result["withLocation"])
        self.assertNotIn("<dt>Location</dt>", result["withoutLocation"])


class CorrectionFrontendTests(unittest.TestCase):
    def run_js(self, functions, body):
        source = (Path(__file__).parents[1] / "src/archive_index/web/app.js").read_text(encoding="utf-8")
        parts = []
        for name, following in functions:
            end = source.index("function " + following)
            if source[end-6:end] == "async ": end -= 6
            parts.append(source[source.index("function " + name):end])
        result = subprocess.run([shutil.which("node"), "--eval", "const escapeHtml=String;" + "\n".join(parts) + body],capture_output=True,text=True,encoding="utf-8",check=True)
        return json.loads(result.stdout)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_folder_histories_and_pluralization(self):
        result=self.run_js([("normalizeFolders", "renderFolderTree")], """
        const state={folderPaths:['','parent','parent/child'],folders:null};
        toggleFolder('parent',false);
        const parentOnly={paths:[...state.folders].sort(),summary:folderSummary()};
        state.folders=new Set(['']); normalizeFolders();
        const rootOnly={paths:[...state.folders].sort(),summary:folderSummary()};
        state.folders=new Set(['parent/child']); normalizeFolders();
        const childOnly={paths:[...state.folders].sort(),summary:folderSummary()};
        state.folders=new Set(['parent','parent/child']); normalizeFolders();
        const parentChild={paths:[...state.folders].sort(),summary:folderSummary()};
        state.folders=new Set(['','parent','parent/child']); normalizeFolders();
        const all=folderSummary();
        state.folders=new Set();
        console.log(JSON.stringify({rootOnly,parentOnly,childOnly,parentChild,all,none:folderSummary()}));
        """)
        self.assertEqual(result["rootOnly"],{"paths":[""],"summary":"1 folder selected"})
        self.assertEqual(result["parentOnly"],{"paths":["","parent/child"],"summary":"2 folders selected"})
        self.assertEqual(result["childOnly"],{"paths":["parent/child"],"summary":"1 folder selected"})
        self.assertEqual(result["parentChild"],{"paths":["parent","parent/child"],"summary":"2 folders selected"})
        self.assertEqual(result["all"],"All folders selected")
        self.assertEqual(result["none"],"0 folders selected")

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
        const rawExpected=renderRepresentations({physical_files:[{...base,extension:'.arw',filename:'a.arw',components:{metadata:{status:'unsupported',error:'no image decoder is configured for .arw'},thumbnail:{status:'unsupported',error:'no image decoder is configured for .arw'}}}]});
        const rawRequested=renderRepresentations({physical_files:[{...base,extension:'.arw',filename:'a.arw',components:{quality:{status:'failed',error:'RAW preview extraction failed'}}}]});
        console.log(JSON.stringify({healthy,warning,rawExpected,rawRequested}));
        """
        result=json.loads(subprocess.run([shutil.which('node'),'--eval',script],check=True,capture_output=True,text=True,encoding="utf-8").stdout)
        self.assertNotIn('<details',result['healthy'])
        self.assertIn('a.jpg', result['healthy'])
        self.assertNotIn('JPEG ·', result['healthy'])
        self.assertIn('Representation status',result['warning'])
        self.assertIn('RAW decoder unavailable',result['warning'])
        self.assertNotIn('<details',result['rawExpected'])
        self.assertIn('RAW preview extraction failed',result['rawRequested'])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_details_omit_video_samples_and_representation_prefixes(self):
        source=(Path(__file__).parents[1]/"src"/"archive_index"/"web/app.js").read_text(encoding="utf-8")
        render_details=source[source.index("function renderDetails"):source.index("function renderRepresentations")]
        render_representations=source[source.index("function renderRepresentations"):source.index("function renderQuality")]
        render_quality=source[source.index("function renderQuality"):source.index("function meterMarkup")]
        script="const escapeHtml=String,formatBytes=String,formatCapture=()=>'',renderTechnicalDetails=()=>'',renderComponentProblems=()=>'',qualityColor=()=>'#fff',renderQuality=" + "(" + render_quality + ")," + "renderRepresentations=" + "(" + render_representations + ");" + render_details + "const asset={capture_time:null,physical_files:[{filename:'clip.mp4',extension:'.mp4',size_bytes:100,is_preferred:true,is_online:true,media_type:'video',video_quality:{successful_count:30,requested_count:30},quality_score:.8}]}; console.log(renderDetails(asset));"
        result=subprocess.run([shutil.which('node'),'--eval',script],check=True,capture_output=True,text=True,encoding="utf-8").stdout
        self.assertNotIn("Video samples:", result)
        self.assertNotIn("Physical file", result)
        self.assertIn("clip.mp4", result)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_disabled_quality_is_omitted_but_requested_failure_remains(self):
        source = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app.js").read_text(encoding="utf-8")
        render_quality = source[source.index("function renderQuality"):source.index("function meterMarkup")]
        script = "const escapeHtml=String,qualityColor=()=>'#fff';" + render_quality + """
        const image = renderQuality({media_type:'image', quality_score:null, components:{quality:{status:'not_requested'}}}, false);
        const video = renderQuality({media_type:'video', quality_score:null, components:{quality:{status:'not_requested'}}}, false);
        const failed = renderQuality({media_type:'video', quality_score:null, components:{quality:{status:'failed', error:'decode failed'}}}, false);
        console.log(JSON.stringify({image,video,failed}));
        """
        result = json.loads(subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
        self.assertEqual((result["image"], result["video"]), ("", ""))
        self.assertIn("Technical quality scoring failed", result["failed"])

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
        self.assertIn('left:33.333%',result['normal'])
        self.assertIn('left:66.667%',result['normal'])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_backdrop_detection_ignores_dialog_scrollbar_and_content(self):
        result=self.run_js([("bindBackdropClose", "showDetails")], """
        const events={};const closed=[];
        const dialog={getBoundingClientRect:()=>({left:100,top:100,right:500,bottom:500}),addEventListener:(name,handler)=>events[name]=handler};
        const closeDialog=()=>closed.push(true);bindBackdropClose(dialog);
        events.pointerdown({target:dialog,clientX:490,clientY:300});events.pointerup({target:dialog,clientX:490,clientY:300});
        events.pointerdown({target:dialog,clientX:20,clientY:20});events.pointerup({target:dialog,clientX:20,clientY:20});
        events.pointerdown({target:dialog,clientX:20,clientY:20});events.pointerup({target:dialog,clientX:200,clientY:200});
        console.log(JSON.stringify(closed));
        """)
        self.assertEqual(result,[True])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_gallery_never_renders_empty_state_while_search_is_pending(self):
        result=self.run_js([("renderGalleryWindow", "loadAssets")], """
        const gallery={style:{setProperty(){},},innerHTML:'',setAttribute(){}};const $=()=>gallery;const bindGalleryCards=()=>{};const state={semanticPending:true,galleryLoading:false};
        renderGalleryWindow({items:[],has_next:false,search:{state:'complete'}},0,1,360);const pending=gallery.innerHTML;
        state.semanticPending=false;renderGalleryWindow({items:[],has_next:false,search:{state:'complete'}},0,1,360);console.log(JSON.stringify({pending,complete:gallery.innerHTML}));
        """)
        self.assertIn("Loading media", result["pending"])
        self.assertNotIn("No media match", result["pending"])
        self.assertIn("No media match", result["complete"])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_initial_gallery_load_fetches_legacy_assets_with_semantic_disabled(self):
        source = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app.js").read_text(encoding="utf-8")
        gallery = source[source.index("async function loadAssets"):source.index("function bindGalleryCards")]
        script = gallery + """
        const galleryNode={clientWidth:900,getBoundingClientRect:()=>({top:0})};const $=()=>galleryNode;
        const window={scrollY:0,innerHeight:720};const filterParams=()=>new URLSearchParams({semantic:'0'});const syncUrl=()=>{};
        const bindGalleryCards=()=>{};let rendered=null;const renderGalleryWindow=data=>rendered=data.items.map(item=>item.filename);
        const browserData=async params=>({items:[{filename:'legacy.jpg'}],total:1,has_next:false,search:{state:'complete'},semantic:params.get('semantic')});
        const state={semanticEnabled:false,semanticPending:false,galleryLoading:true,galleryRequestInFlight:false,assetRequest:0,renderKeys:{},browserAbort:null,searchPoll:null,items:[],total:0,windowStart:0,windowHasNext:false,windowColumns:1,windowHeight:360};
        loadAssets().then(()=>console.log(JSON.stringify({rendered,semantic:state.renderKeys.gallery.includes('semantic=0')})));
        """
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(result.stdout), {"rendered": ["legacy.jpg"], "semantic": True})

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_initial_gallery_load_matrix_renders_current_and_legacy_workspaces(self):
        source = (Path(__file__).parents[1] / "src/archive_index/web/app.js").read_text(encoding="utf-8")
        gallery = source[source.index("async function loadAssets"):source.index("function bindGalleryCards")]
        script = gallery + """
        const galleryNode={clientWidth:900,getBoundingClientRect:()=>({top:0})};const $=()=>galleryNode;
        const window={scrollY:0,innerHeight:720};const syncUrl=()=>{};const bindGalleryCards=()=>{};
        const cases=[['current',true],['current',false],['legacy',true],['legacy',false]];const output=[];let state,semanticEnabled,workspace,rendered,requests;
        const filterParams=()=>new URLSearchParams({semantic:semanticEnabled?'1':'0'});
        const renderGalleryWindow=data=>{rendered=data.items.map(item=>item.filename)};
        const browserData=async params=>{requests++;return {items:[{filename:`${workspace}-${semanticEnabled?'on':'off'}.jpg`}],total:1,has_next:false,search:{state:'complete'},semantic:params.get('semantic')}};
        for (const current of cases) {
          [workspace,semanticEnabled]=current;
          state={semanticEnabled,semanticPending:false,galleryLoading:true,galleryRequestInFlight:false,assetRequest:0,renderKeys:{},browserAbort:null,searchPoll:null,items:[],total:0,windowStart:0,windowHasNext:false,windowColumns:1,windowHeight:360};
          rendered=null;requests=0;
          await loadAssets();
          output.push({workspace,semanticEnabled,rendered,requests,semantic:state.renderKeys.gallery.includes(`semantic=${semanticEnabled?'1':'0'}`)});
        }
        console.log(JSON.stringify(output));
        """
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(result.stdout), [
            {"workspace": "current", "semanticEnabled": True, "rendered": ["current-on.jpg"], "requests": 1, "semantic": True},
            {"workspace": "current", "semanticEnabled": False, "rendered": ["current-off.jpg"], "requests": 1, "semantic": True},
            {"workspace": "legacy", "semanticEnabled": True, "rendered": ["legacy-on.jpg"], "requests": 1, "semantic": True},
            {"workspace": "legacy", "semanticEnabled": False, "rendered": ["legacy-off.jpg"], "requests": 1, "semantic": True},
        ])

    def test_workspace_bootstrap_orders_initial_gallery_load_after_jobs(self):
        source = (Path(__file__).parents[1] / "src/archive_index/web/app.js").read_text(encoding="utf-8")
        start = source.index("async function loadWorkspace")
        end = source.index("function showSearchStatus", start)
        workspace = source[start:end]
        self.assertLess(workspace.index("await loadJobs()"), workspace.index("if (state.viewMode === \"groups\") await loadGroups(); else await loadAssets();"))
        self.assertNotIn("const initialLoad", workspace)
        self.assertIn("loadWorkspace().then(() => setInterval", source)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_stale_job_bootstrap_cannot_replace_rendered_gallery(self):
        source = (Path(__file__).parents[1] / "src/archive_index/web/app.js").read_text(encoding="utf-8")
        start = source.index("async function loadJobs")
        end = source.index("async function loadProblemsBadge", start)
        script = source[start:end] + r'''
        const gallery={innerHTML:"<article class='photo-card'>card</article>"};
        const nodes={search:{value:""},"workspace-view":{classList:{contains:()=>false}},"job-banner":{classList:{add(){},remove(){}}},"job-copy":{},"job-progress":{},"cancel-job":{}};
        const $=id=>id==="gallery"?gallery:nodes[id];const showSearchStatus=()=>{};const loadGroups=async()=>{};const loadProblemsBadge=async()=>{};
        const state={jobsRequest:0,browserRevision:null,renderKeys:{},activeJobId:"job-1",viewMode:"gallery"};let jobCalls=0;
        const api=async path=>{if(path.startsWith("/api/jobs")) return jobCalls++ ? {revision:"new",jobs:[{id:"job-1",status:"running",total_items:1,completed_items:1}]} : {revision:"old",jobs:[]};if(path==="/api/search-status") return {state:"ready"};if(path==="/api/workspace"){await new Promise(resolve=>setTimeout(resolve,10));return {}};throw new Error(path)};
        const loadAssets=async()=>{gallery.innerHTML="<div class='empty'>No media match these filters.</div>"};
        (async()=>{const first=loadJobs();await Promise.resolve();const second=loadJobs();await Promise.all([first,second]);console.log(JSON.stringify({gallery:gallery.innerHTML,state:state.activeJobId}))})();
        '''
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        value = json.loads(result.stdout)
        self.assertIn("photo-card", value["gallery"])
        self.assertNotIn("No media match", value["gallery"])
        self.assertEqual(value["state"], "job-1")

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_search_helper_states_and_error_style(self):
        result=self.run_js([("showSearchStatus","prepareSearch")], """
        const nodes={};const $=id=>nodes[id] ||= {dataset:{},textContent:''};const state={};
        const states=[['ready','Semantic search ready · OpenCLIP'],['loading','Loading OpenCLIP…'],['searching','Searching…'],['complete',''],['missing_model','OpenCLIP model not installed'],['missing_embeddings','Embeddings not indexed · Re-index required'],['failed','Search failed: checkpoint unreadable']];
        const out=states.map(([phase,message])=>{showSearchStatus({state:phase,message},{media_shown:147,workspace_total:550,query_active:false,filename_matches:0});return {phase:$('search-status').dataset.state,text:$('search-status').textContent,count:$('search-count').textContent}});
        console.log(JSON.stringify(out));
        """)
        self.assertEqual(result[0]['phase'],'ready')
        self.assertEqual(result[1]['text'],'Loading OpenCLIP…')
        self.assertEqual(result[2]['text'],'Searching…')
        self.assertEqual(result[3]['text'],'Semantic search complete')
        self.assertEqual(result[3]['count'],'147 / 550 media shown')
        self.assertEqual(result[-1]['phase'],'failed')
        self.assertIn('checkpoint unreadable',result[-1]['text'])

    def test_technical_details_uses_single_column_and_collapses_missing_camera_cleanly(self):
        source = (Path(__file__).parents[1] / "src/archive_index/web/app.js").read_text(encoding="utf-8")
        css = (Path(__file__).parents[1] / "src/archive_index/web/app.css").read_text(encoding="utf-8")
        self.assertIn("No camera info available", source)
        self.assertNotIn("grid-template-columns:max-content minmax(0,1fr) max-content minmax(0,1fr)", css)
        self.assertIn("#details .technical-details .kv { grid-template-columns:minmax(130px,.7fr) minmax(0,1.3fr);", css)
        self.assertIn("#details .detail-layout { position:relative; }", css)
        self.assertNotIn("#details .detail-layout { display:grid", css)
        self.assertNotIn("#details .detail-overview { min-height", css)
        self.assertIn(".viewer-quality { position:absolute;", css)
        self.assertNotIn(".viewer-technical-quality { display:grid", css)

    def test_folder_picker_is_single_instance_and_restores_button_state(self):
        source = (Path(__file__).parents[1] / "src/archive_index/web/app.js").read_text(encoding="utf-8")
        start = source.index("async function pickWorkspace()")
        end = source.index("function folderRule", start)
        picker = source[start:end]
        self.assertIn("workspacePickerInFlight", source)
        self.assertIn("if (workspacePickerInFlight) return;", picker)
        self.assertIn("button.disabled = true", picker)
        self.assertIn("button.disabled = false", picker)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_similarity_and_quality_color_endpoints(self):
        result=self.run_js([("qualityColor","scoreMarkup"),("similarityMarkup","selectionState")], """
        console.log(JSON.stringify({quality:[qualityColor(.2),qualityColor(.4),qualityColor(.7),qualityColor(1)],image:similarityMarkup({similarity:0}),text:similarityMarkup({similarity:.18,similarity_kind:'text'}),green:similarityMarkup({similarity:.35,similarity_kind:'text'})}));
        """)
        self.assertEqual(result['quality'][0],result['quality'][1])
        self.assertEqual(result['quality'][2],'rgb(205, 185, 222)')
        self.assertEqual(result['quality'][3],'rgb(169, 224, 239)')
        self.assertNotEqual(result['quality'][1],result['quality'][2])
        self.assertIn('--similarity-color: rgb(217, 105, 105)',result['image'])
        self.assertIn('--similarity-color: rgb(231, 198, 85)',result['text'])
        self.assertIn('--similarity-color: rgb(91, 190, 118)',result['green'])

    def test_windowed_similar_loading_and_semantic_disabled_hooks(self):
        source=(Path(__file__).parents[1]/"src/archive_index/web/app.js").read_text(encoding="utf-8")
        html=(Path(__file__).parents[1]/"src/archive_index/web/index.html").read_text(encoding="utf-8")
        self.assertIn("async function loadSimilarPage",source)
        self.assertIn("id=\"similar-more\"",source)
        self.assertIn("similar.items = [...similar.items, ...data.items]",source)
        self.assertNotIn("slice(-18)",source)
        self.assertIn("similar.autoLoad",source)
        self.assertIn("Close similar images",source)
        self.assertIn("limit = similar.initial ? 12 : 6",source)
        self.assertIn("&initial=",source)
        self.assertIn("function showToast",source)
        self.assertIn("Gallery request failed",source)
        self.assertIn("Similar images failed",source)
        self.assertIn('id="toast-region"',html)
        self.assertIn('$("viewer").onscroll',source)
        self.assertIn('id="similarity-control" class="hidden"',html)
        self.assertIn('id="search-sort-button" class="hidden"',html)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_similar_paging_appends_all_ranked_pages(self):
        source=(Path(__file__).parents[1]/"src/archive_index/web/app.js").read_text(encoding="utf-8")
        render=source[source.index("function renderSimilarResults"):source.index("function closeSimilar")]
        loader=source[source.index("async function loadSimilarPage"):source.index("async function showSimilar")]
        script="const escapeHtml=String;"+render+loader+"""
        const section={innerHTML:'',querySelectorAll:()=>[],classList:{},scrollHeight:100,clientHeight:500,addEventListener(){}};
        const $=id=>section;const requestAnimationFrame=()=>{};const similarityMarkup=()=>'';const closeSimilar=()=>{};
        const state={similar:{assetId:'source',items:[],offset:0,total:0,strongCount:0,hasNext:true,loading:false,autoLoad:true,error:null}};
        let calls=0;const api=async()=>{const start=calls++*6;return {items:Array.from({length:6},(_,index)=>({filename:`item-${start+index}`})),total:24,strong_count:24,has_next:calls<4};};
        (async()=>{for(let i=0;i<4;i++) await loadSimilarPage();console.log(JSON.stringify({calls,items:state.similar.items.map(item=>item.filename)}));})();
        """
        completed=subprocess.run([shutil.which("node"),"--eval",script],capture_output=True,text=True,encoding="utf-8")
        self.assertEqual(completed.returncode,0,completed.stderr)
        result=json.loads(completed.stdout)
        self.assertEqual(result["calls"],4)
        self.assertEqual(result["items"],[f"item-{index}" for index in range(24)])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_gallery_bottom_scroll_keeps_virtual_window_at_end(self):
        source=(Path(__file__).parents[1]/"src/archive_index/web/app.js").read_text(encoding="utf-8")
        gallery=source[source.index("async function loadAssets"):source.index("function bindGalleryCards")]
        scroll=source[source.index("let scrollTimer;"):source.index('$("viewer").addEventListener("cancel"')]
        script=gallery+"""
        const galleryNode={clientWidth:900,open:false,classList:{contains:()=>false},getBoundingClientRect:()=>({top:100-window.scrollY}),style:{setProperty(){}},setAttribute(){}};
        const $=()=>galleryNode;const filterParams=()=>new URLSearchParams("q=test");const syncUrl=()=>{};const renderGalleryWindow=()=>{};const bindGalleryCards=()=>{};
        const total=92;const calls=[];const browserData=async params=>{const offset=Number(params.get("offset"));calls.push(offset);const count=Math.min(28,total-offset);return {items:Array.from({length:Math.max(0,count)},(_,index)=>({index:offset+index})),total,has_next:offset+count<total,search:{state:"complete"}};};
        const listeners={};const window={scrollY:0,innerHeight:720,scrollTo(x,y){this.scrollY=y},addEventListener(name,handler){listeners[name]=handler}};
        const clearTimeout=()=>{};const setTimeout=(handler)=>{handler();return 0};
        const state={viewMode:"gallery",assetRequest:0,renderKeys:{},browserAbort:null,searchPoll:null,semanticPending:false,galleryLoading:false,galleryRequestInFlight:false,items:[],total:0,windowStart:0,windowColumns:1,windowHeight:360,windowHasNext:false};
        """+scroll+"""
        (async()=>{
          await loadAssets(0);
          for(let page=0;page<6;page++){
            window.scrollY=100+(state.windowStart+state.items.length)/state.windowColumns*state.windowHeight;
            listeners.scroll();
            await new Promise(resolve=>setImmediate(resolve));
          }
          const before=calls.length;
          const finalScroll=window.scrollY;
          for(let repeat=0;repeat<3;repeat++){listeners.scroll();await new Promise(resolve=>setImmediate(resolve));}
          console.log(JSON.stringify({calls,afterBottomCalls:calls.length-before,first:state.items[0].index,last:state.items.at(-1).index,windowStart:state.windowStart,scroll:window.scrollY,finalScroll}));
        })();
        """
        result=subprocess.run([shutil.which("node"),"--eval",script],capture_output=True,text=True,encoding="utf-8")
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual(value["calls"],[0,12,24,36,48,60,72])
        self.assertEqual(value["afterBottomCalls"],0)
        self.assertEqual((value["first"],value["last"],value["windowStart"]),(72,91,72))
        self.assertEqual(value["scroll"],value["finalScroll"])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_initial_short_gallery_does_not_prefetch_next_window(self):
        source=(Path(__file__).parents[1]/"src/archive_index/web/app.js").read_text(encoding="utf-8")
        gallery=source[source.index("async function loadAssets"):source.index("function bindGalleryCards")]
        script=gallery+"""
        const galleryNode={clientWidth:900,getBoundingClientRect:()=>({top:0}),style:{setProperty(){}},setAttribute(){}};
        const $=()=>galleryNode;const filterParams=()=>new URLSearchParams("q=test");const syncUrl=()=>{};const rendered=[];const renderGalleryWindow=data=>rendered.push(data.items.map(item=>item.filename));const bindGalleryCards=()=>{};
        let calls=0;const browserData=async()=>{calls+=1;return {items:[{filename:calls===1?'first.jpg':'second-window.jpg'}],total:2,has_next:calls===1,search:{state:"complete"}};};
        const window={scrollY:0,innerHeight:720,scrollTo(){}};const document={documentElement:{scrollHeight:720}};
        const state={assetRequest:0,renderKeys:{},browserAbort:null,searchPoll:null,semanticPending:false,galleryLoading:false,galleryRequestInFlight:false,items:[],total:0,windowStart:0,windowColumns:1,windowHeight:360,windowHasNext:false};
        (async()=>{await loadAssets();await new Promise(resolve=>setImmediate(resolve));console.log(JSON.stringify({calls,rendered,first:state.items[0].filename}));})();
        """
        result=subprocess.run([shutil.which("node"),"--eval",script],capture_output=True,text=True,encoding="utf-8")
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{"calls":1,"rendered":[["first.jpg"]],"first":"first.jpg"})

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_cached_gallery_and_group_windows_skip_fetch(self):
        source=(Path(__file__).parents[1]/"src/archive_index/web/app.js").read_text(encoding="utf-8")
        gallery=source[source.index('async function loadAssets'):source.index('function bindGalleryCards')]
        groups=source[source.index('async function loadGroups'):source.index('function renderGroupPager')]
        script=gallery+groups+"""
        const state={assetRequest:0,groupRequest:0,groupPage:1,renderKeys:{},galleryRequestInFlight:false,items:[],total:0,windowStart:0,windowHasNext:false};
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
