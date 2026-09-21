from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


WEB_SCRIPT_NAMES = (
    "app-shared.js",
    "app-setup.js",
    "app-browser.js",
    "app-viewer.js",
    "app-details.js",
    "app-maintenance.js",
    "app-groups.js",
    "app-visualizations.js",
    "app-bootstrap.js",
)


def javascript_source() -> str:
    root = Path(__file__).parents[1] / "src" / "archive_index" / "web"
    return "\n".join((root / name).read_text(encoding="utf-8") for name in WEB_SCRIPT_NAMES)


class FrontendTests(unittest.TestCase):
    def test_semantic_search_and_similar_ui_hooks_are_present(self) -> None:
        root = Path(__file__).parents[1] / "src" / "archive_index" / "web"
        html = (root / "index.html").read_text(encoding="utf-8")
        javascript = javascript_source()
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
        self.assertIn('id="forget-offline-button"', html)
        self.assertIn('id="offline-dialog"', html)
        self.assertIn("This does not delete files from disk.", javascript)
        self.assertIn("/api/offline-media/forget", javascript)
        self.assertNotIn("image_extensions", html)
        self.assertIn("/api/browser?", javascript)
        self.assertIn("Show similar assets", html)
        self.assertIn("Show image group", html)
        self.assertIn("similarity-chip", javascript)
        self.assertIn("similarity-chip", css)
        self.assertIn('Index</span> <span class="setup-accent">these folders:</span>', html)
        self.assertNotIn("1 —", html)
        self.assertNotIn("2 —", html)
        self.assertNotIn("3 —", html)
        self.assertNotIn("4 —", html)
        self.assertNotIn("Every supported media format", html)
        self.assertNotIn("Enable semantic search", html)
        self.assertNotIn("Models are installed explicitly", html)
        self.assertNotIn("Estimated from local completed-job timings", javascript)
        self.assertEqual(html.count('<span>Automatically</span><span class="setup-accent"> assess media quality</span>'), 1)
        self.assertEqual(html.count('<span>Let me</span><span class="setup-accent"> search in plain English</span>'), 1)
        self.assertNotIn("Let me search by semantic content", html)
        self.assertIn("setup-accent", javascript)
        self.assertIn("Estimated indexing time", javascript)
        self.assertEqual(html.count('id="setup-quality"'), 1)
        self.assertEqual(html.count('id="setup-semantic-search"'), 1)
        self.assertEqual(html.count('id="setup-video-participation"'), 1)
        self.assertIn("class=\"model-card", html)
        self.assertNotIn("<h2>Diagnostics</h2>", html)
        self.assertNotIn("Intentionally skipped work is not a problem", html)

    def test_configure_accent_fragments_are_inverted(self) -> None:
        source = javascript_source()
        self.assertIn('<span>Assess</span> <span class="setup-accent">video</span> <span>quality and</span> <span class="setup-accent">include videos</span> <span>in semantic search</span> <span class="setup-accent">too</span>', source)
        self.assertIn('<span>Assess</span> <span class="setup-accent">video</span> <span>quality</span> <span class="setup-accent">too</span>', source)
        self.assertIn('<span class="setup-accent">Include videos</span> <span>in semantic search</span> <span class="setup-accent">too</span>', source)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_home_cards_format_counts_timestamps_and_thumbnails(self) -> None:
        source=javascript_source()
        css=(Path(__file__).parents[1]/"src/archive_index/web/app.css").read_text(encoding="utf-8")
        timestamp=source[source.index("function formatHomeTimestamp"):source.index("function workspaceAssetCount")]
        count=source[source.index("function workspaceAssetCount"):source.index("function scoreMarkup")]
        script=timestamp+count+"console.log(JSON.stringify({time:formatHomeTimestamp('2026-09-18T23:27:12+03:00'),one:workspaceAssetCount(1),many:workspaceAssetCount(2)}));"
        environment = os.environ.copy()
        environment["TZ"] = "UTC"
        result=json.loads(subprocess.run([shutil.which("node"),"--eval",script],capture_output=True,text=True,encoding="utf-8",env=environment,check=True).stdout)
        self.assertEqual(result["time"], "18/09/2026 at 20:27")
        self.assertIn('<strong class="workspace-asset-count">1</strong> indexed asset', result["one"])
        self.assertIn('<strong class="workspace-asset-count">2</strong> indexed assets', result["many"])
        self.assertIn("home-active", source)
        self.assertIn('class="recent-thumb" loading="lazy"', source)
        self.assertIn("workspace.thumbnail_url", source)
        self.assertNotIn("${workspace.assets ?? 0} indexed assets", source)
        self.assertIn('workspace-indexed-time">Indexed on', source)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(min(100%, 440px), 1fr))", css)
        self.assertIn("width: 8.5rem; height: 8.5rem", css)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_eta_format_and_viewer_close_contract(self) -> None:
        source = javascript_source()
        start = source.index("function formatEta")
        end = source.index("function renderSetupPlanData", start)
        result = subprocess.run(
            [shutil.which("node"), "--eval", source[start:end] + "console.log(JSON.stringify([formatEta(0), formatEta(0.2), formatEta(98.1), formatEta(NaN)]));"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), ["ready", "00:01", "01:39", "ready"])
        self.assertIn('active.eta_seconds', source)
        self.assertIn('const stageEtaSeconds = sub.eta ?? active.eta_seconds;', source)
        self.assertIn('$("viewer-close").addEventListener("click", () => closeDialog($("viewer")));', source)
        self.assertNotIn('state.viewerContext === "similar" && state.similarSource', source)
        self.assertIn('event.preventDefault(); closeDialog($("viewer"));', source)
        css = (Path(__file__).parents[1] / "src/archive_index/web/app.css").read_text(encoding="utf-8")
        self.assertIn("progress { width: 100%; height: 1rem;", css)
        self.assertIn(".setup-eta-value { color:#a9c9e6; }", css)
        self.assertIn('class="setup-eta-value"', source)
        self.assertNotIn('$("setup-index-summary").textContent', source)

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
        source = javascript_source()
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
        self.assertIn("const delay = state.searchPollCount < 3 ? 275 : 500", source)
        self.assertIn("generation === state.searchGeneration", source)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_recommended_tag_replaces_representative_tag(self):
        source=javascript_source()
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
        source = javascript_source()
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
        source = javascript_source()
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
        source = javascript_source()
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
        source = javascript_source()
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
        self.assertIn("<svg", result["withLocation"])
        self.assertNotIn(">↗</a>", result["withLocation"])
        self.assertNotIn("<dt>Location</dt>", result["withoutLocation"])


class CorrectionFrontendTests(unittest.TestCase):
    def run_js(self, functions, body):
        source = javascript_source()
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
        source=javascript_source()
        render=source[source.index("function renderViewer"):source.index("function applyViewerTransform")]
        self.assertLess(render.index("stopViewerMedia()"),render.index('$("viewer-media").innerHTML = ""'))
        drawer=source[source.index("async function toggleViewerInfo"):source.index("async function loadJobs")]
        self.assertNotIn("renderViewer",drawer)
        self.assertNotIn("stopViewerMedia",drawer)
        self.assertIn('addEventListener("close", stopViewerMedia)',source)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_viewer_paging_keeps_absolute_asset_identity_across_sixty_item_boundaries(self):
        source = javascript_source()
        start = source.index("async function moveViewer")
        end = source.index("function stopViewerMedia", start)
        result = subprocess.run(
            [shutil.which("node"), "--eval", source[start:end] + """
            const records=Array.from({length:240},(_,index)=>({asset_id:'asset-'+index,filename:'asset-'+index}));
            const state={viewerContext:'gallery',viewerStart:0,viewerIndex:59,viewerItems:records.slice(0,60),viewerTotal:240,viewerPageSize:60};
            const filterParams=()=>new URLSearchParams();
            const api=async path=>{const query=new URL('http://localhost/'+path).searchParams;const offset=Number(query.get('offset'));const limit=Number(query.get('limit'));return {items:records.slice(offset,offset+limit),total:records.length};};
            const resetViewerZoom=()=>{};const renderViewer=()=>{};
            const $=()=>({textContent:''});
            const visited=[];
            for(let index=0;index<151;index++){await moveViewer(1);visited.push(state.viewerItems[state.viewerIndex].asset_id);}
            for(let index=0;index<150;index++){await moveViewer(-1);}
            console.log(JSON.stringify({first:visited[0],last:visited.at(-1),unique:new Set(visited).size,final:state.viewerItems[state.viewerIndex].asset_id,ordinal:state.viewerStart+state.viewerIndex+1}));
            """],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        result = json.loads(result.stdout)
        self.assertEqual(result, {"first":"asset-60","last":"asset-210","unique":151,"final":"asset-60","ordinal":61})

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_representation_diagnostics_only_when_present(self):
        source=javascript_source()
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
        source=javascript_source()
        render_details=source[source.index("function renderDetails"):source.index("function renderRepresentations")]
        render_representations=source[source.index("function renderRepresentations"):source.index("function renderQuality")]
        render_quality=source[source.index("function renderQuality"):source.index("function meterMarkup")]
        script="const escapeHtml=String,formatBytes=String,formatCapture=()=>'',renderTechnicalDetails=()=>'',renderComponentProblems=()=>'',qualityColor=()=>'#fff',renderQuality=" + "(" + render_quality + ")," + "renderRepresentations=" + "(" + render_representations + ");" + render_details + "const asset={capture_time:null,physical_files:[{filename:'clip.mp4',extension:'.mp4',size_bytes:100,is_preferred:true,is_online:true,media_type:'video',video_quality:{successful_count:30,requested_count:30},quality_score:.8}]}; console.log(renderDetails(asset));"
        result=subprocess.run([shutil.which('node'),'--eval',script],check=True,capture_output=True,text=True,encoding="utf-8").stdout
        self.assertNotIn("Video samples:", result)
        self.assertNotIn("Physical file", result)
        self.assertIn("clip.mp4", result)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_missing_detail_thumbnail_keeps_clickable_placeholder(self):
        source = javascript_source()
        start = source.index("function renderDetails")
        end = source.index("function renderRepresentations", start)
        function = source[start:end]
        script = f'''
        const escapeHtml=String,formatBytes=String,formatCapture=()=>'',renderTechnicalDetails=()=>'',renderQuality=()=>'',renderRepresentations=()=>'';
        {function}
        console.log(renderDetails({{physical_files:[{{filename:'broken.jxl',relative_path:'broken.jxl',size_bytes:100}}]}}, {{showThumbnail:true}}));
        '''
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True).stdout
        self.assertIn('class="detail-thumbnail placeholder"', result)
        self.assertIn('data-detail-thumbnail', result)
        self.assertIn("Preview unavailable", result)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_disabled_quality_is_omitted_but_requested_failure_remains(self):
        source = javascript_source()
        render_quality = source[source.index("function renderQuality"):source.index("function meterMarkup")]
        script = "const escapeHtml=String,qualityColor=()=>'#fff';" + render_quality + """
        const image = renderQuality({media_type:'image', quality_score:null, components:{quality:{status:'not_requested'}}}, false);
        const video = renderQuality({media_type:'video', quality_score:null, components:{quality:{status:'not_requested'}}}, false);
        const failed = renderQuality({media_type:'video', quality_score:null, components:{quality:{status:'failed', error:'decode failed'}}}, false);
        console.log(JSON.stringify({image,video,failed}));
        """
        result = json.loads(subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
        self.assertEqual((result["image"], result["video"]), ("", ""))
        self.assertIn("Overall technical quality", result["failed"])
        self.assertIn(">N/A</span>", result["failed"])
        self.assertNotIn("decode failed", result["failed"])

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
    def test_focal_length_prefers_valid_35mm_equivalent(self):
        source = javascript_source()
        functions = source[source.index("function meterMarkup"):source.index("async function loadViewerDetails")]
        script = "const escapeHtml=String;" + functions + """
        const base={metadata:{exif:{FocalLength:[554,100],FocalLengthIn35mmFilm:23}}};
        const absent={metadata:{exif:{FocalLength:[554,100]}}};
        const invalid={metadata:{exif:{FocalLength:[554,100],FocalLengthIn35mmFilm:'0/0'}}};
        console.log(JSON.stringify({equivalent:renderTechnicalDetails(base),absent:renderTechnicalDetails(absent),invalid:renderTechnicalDetails(invalid)}));
        """
        result = json.loads(subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
        self.assertIn("Focal length (35mm eq.)", result["equivalent"])
        self.assertIn("23 mm", result["equivalent"])
        self.assertNotIn("5.54 mm", result["equivalent"])
        self.assertIn("Focal length", result["absent"])
        self.assertIn("5.54 mm", result["absent"])
        self.assertIn("Focal length", result["invalid"])
        self.assertIn("5.54 mm", result["invalid"])

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
        source = javascript_source()
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
        source = javascript_source()
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
        source = javascript_source()
        start = source.index("async function loadWorkspace")
        end = source.index("function showSearchStatus", start)
        workspace = source[start:end]
        self.assertLess(workspace.index("await loadJobs()"), workspace.index("await loadCurrentView();"))
        self.assertNotIn("const initialLoad", workspace)
        self.assertIn("loadWorkspace().then(() => setInterval", source)

    def test_visualization_navigation_and_canvas_contract(self):
        root = Path(__file__).parents[1] / "src" / "archive_index" / "web"
        html = (root / "index.html").read_text(encoding="utf-8")
        source = javascript_source()
        visualizations = (root / "app-visualizations.js").read_text(encoding="utf-8")
        for label in ("Gallery", "Groupings", "Geo Map", "Timeline", "Vector Cloud"):
            self.assertIn(label, html)
        self.assertNotIn("Cloud Map", html)
        self.assertNotIn("Not implemented yet", html)
        self.assertIn('id="visualization-selection-panel"', html)
        self.assertIn("timeline-mode-capture", html)
        self.assertIn("timeline-mode-file-created", html)
        self.assertIn("visualizationCapabilities", source)
        self.assertIn("loadVisualizationCapabilities", source)
        self.assertIn("similarSourceId", source)
        self.assertIn("item.asset_id !== similarSourceId", source)
        self.assertIn("async function loadCurrentView()", source)
        self.assertIn("/api/visualizations/", visualizations)
        self.assertIn('getContext("2d")', visualizations)
        self.assertIn("representativeVisualizationPoint", visualizations)
        self.assertIn("visualization-selection-panel", visualizations)
        self.assertNotIn("stableLane", visualizations)
        self.assertIn("timelineBucketInterval", visualizations)
        self.assertIn("timelineLod", visualizations)
        self.assertIn("buildTimelineCache", visualizations)
        self.assertIn("scheduleVisualizationRender", visualizations)
        self.assertIn("vectorCellKey", visualizations)
        self.assertIn("buildVectorLodCache", visualizations)
        self.assertIn("drawVectorDensity", visualizations)
        self.assertIn("visualization-full-height", html)
        self.assertIn("drawVisualizationBadge", visualizations)
        self.assertIn("vectorDensityRasterPoint", visualizations)
        self.assertNotIn("Math.floor(screen.x / clusterCellSize", visualizations)
        self.assertNotIn("fillRect(column", visualizations)
        self.assertNotIn("thumbnailTier", visualizations)
        self.assertNotIn("PCA 1", visualizations)
        self.assertNotIn("PCA 2", visualizations)
        self.assertIn('id="visualization-selection-panel"', html)
        for remote_map_reference in ("tile.openstreetmap", "mapbox", "google.com/maps"):
            self.assertNotIn(remote_map_reference, visualizations)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_visualization_lod_keys_are_world_or_time_anchored(self):
        visualizations = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app-visualizations.js").read_text(encoding="utf-8")
        snippets = []
        for start_marker, end_marker in (
            ("function timelineBucketInterval", "function timelineX"),
            ("function vectorCellKey", "function fitVector"),
            ("function visualizationThumbnailSize", "function visualizationThumbnail(assetId)"),
        ):
            start = visualizations.index(start_marker)
            end = visualizations.index(end_marker, start)
            snippets.append(visualizations[start:end])
        intervals_start = visualizations.index("const TIMELINE_INTERVALS = ")
        intervals_end = visualizations.index(";", intervals_start) + 1
        script = visualizations[intervals_start:intervals_end] + "\n" + "\n".join(snippets) + r'''
        const intervals = [timelineBucketInterval(86400, 900), timelineBucketInterval(86400, 900)];
        const cells = [vectorCellKey(1.2, -3.4, .5, 0, 0), vectorCellKey(1.2, -3.4, .5, 0, 0)];
        const geoFit=visualizationThumbnailSize({scale:100,baseScale:100},'geo');
        const geoZoom=visualizationThumbnailSize({scale:200,baseScale:100},'geo');
        const timelineFit=visualizationThumbnailSize({visibleSpan:1000,fitVisibleSpan:1000},'timeline');
        const timelineZoom=visualizationThumbnailSize({visibleSpan:500,fitVisibleSpan:1000},'timeline');
        console.log(JSON.stringify({intervals, cells, sizes:[geoFit,geoZoom,timelineFit,timelineZoom]}));
        '''
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        output = json.loads(result.stdout)
        self.assertEqual(output["intervals"][0], output["intervals"][1])
        self.assertEqual(output["cells"][0], output["cells"][1])
        self.assertEqual(output["sizes"], [104, 156, 124, 166])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_timeline_cache_keeps_zero_bins_and_actual_representative_time(self):
        visualizations = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app-visualizations.js").read_text(encoding="utf-8")
        start = visualizations.index("function timelineBucketStart")
        end = visualizations.index("function drawTimelineDensity", start)
        script = "const TIMELINE_KERNEL=[1,4,6,4,1];\n" + visualizations[start:end] + r'''
        function representativeVisualizationPoint(points) { return points[0]; }
        const points=[{asset_id:'a',time:0},{asset_id:'b',time:86400*4}];
        const view={key:'test',timeMode:'capture',timelineCaches:new Map(),centerTime:86400*2,visibleSpan:86400*5,data:{points}};
        const cache=buildTimelineCache(view,points,86400);
        const bins=timelineVisibleBins(cache,0,86400*5-.001);
        const actual=timelineX(view,bins[0].point.time,1000);
        const midpoint=timelineX(view,(bins[0].start+bins[0].end)/2,1000);
        console.log(JSON.stringify({counts:bins.map(bucket=>bucket.count),middle:timelineDensityAt(cache,86400*2),peak:cache.maximum,actual,midpoint,occupied:cache.occupied.size}));
        '''
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        output = json.loads(result.stdout)
        self.assertEqual(output["counts"], [1, 0, 0, 0, 1])
        self.assertLess(output["middle"], output["peak"])
        self.assertNotEqual(output["actual"], output["midpoint"])
        self.assertEqual(output["occupied"], 2)

    def test_visualization_layout_and_world_alignment_contract(self):
        root = Path(__file__).parents[1] / "src" / "archive_index" / "web"
        css = (root / "app.css").read_text(encoding="utf-8")
        visualizations = (root / "app-visualizations.js").read_text(encoding="utf-8")
        self.assertIn(".visualization-view.visualization-full-height", css)
        self.assertNotIn("height:380px", css)
        self.assertIn("timelineX(view, candidate.point.time", visualizations)
        self.assertIn("screenPoint(view, representative.x, representative.y", visualizations)
        self.assertIn("vectorDensityRasterPoint(point, bounds, size)", visualizations)
        self.assertNotIn("calc(100dvh - 112px)", css)

    def test_search_typing_debounces_and_cancels_stale_work(self):
        source = javascript_source()
        self.assertIn("state.browserAbort?.abort(); clearTimeout(state.searchPoll); state.searchGeneration++;", source)
        self.assertIn("debounce = setTimeout", source)
        self.assertIn("}, 250);", source)
        self.assertIn('event.key === "Enter"', source)
        self.assertIn("generation === state.searchGeneration", source)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_visualization_render_scheduler_coalesces_callbacks(self):
        visualizations = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app-visualizations.js").read_text(encoding="utf-8")
        start = visualizations.index("function scheduleVisualizationRender")
        end = visualizations.index("async function loadVisualization", start)
        script = visualizations[start:end] + r'''
        const callbacks=[]; let calls=0;
        const state={viewMode:'timeline',visualizations:{timeline:{data:{},renderFrame:0}}};
        const visualizationView=mode=>state.visualizations[mode]; const renderVisualization=()=>{calls += 1;}; const requestAnimationFrame=callback=>{callbacks.push(callback); return callbacks.length;};
        scheduleVisualizationRender('timeline'); scheduleVisualizationRender('timeline'); callbacks.shift()(0);
        console.log(JSON.stringify({queued:callbacks.length,calls}));
        '''
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(result.stdout), {"queued": 0, "calls": 1})

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_timeline_labels_are_hierarchical_and_wall_clock_stable(self):
        visualizations = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app-visualizations.js").read_text(encoding="utf-8")
        start = visualizations.index("function formatTimelineTick")
        end = visualizations.index("function timelineTicks", start)
        script = visualizations[start:end] + r'''
        const values=[timelineTickLabels(Date.UTC(2026,0,1,0,0,0)/1000,60),timelineTickLabels(Date.UTC(2026,0,2,0,0,0)/1000,60),timelineTickLabels(Date.UTC(2026,0,1,0,0,1)/1000,1),timelineTickLabels(Date.UTC(2026,0,1,0,0,0,123)/1000,.001)];
        console.log(JSON.stringify(values));
        '''
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        values = json.loads(result.stdout)
        self.assertEqual(values[0]["context"], "01 Jan")
        self.assertEqual(values[0]["detail"], "00:00")
        self.assertEqual(values[1]["context"], "02 Jan")
        self.assertEqual(values[2]["detail"], "00:00:01")
        self.assertEqual(values[3]["detail"], "00:00:00.123")

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_vector_density_raster_and_point_share_world_transform(self):
        visualizations = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app-visualizations.js").read_text(encoding="utf-8")
        start = visualizations.index("function vectorDensityRasterPoint")
        end = visualizations.index("function vectorGridStep", start)
        script = r'''
        function screenPoint(view, x, y, width, height) { return {x:(x-view.centerX)*view.scale+width/2,y:(y-view.centerY)*view.scale+height/2}; }
        ''' + visualizations[start:end] + r'''
        const bounds={minX:-2,maxX:2,minY:-1,maxY:3}; const point={x:.5,y:2}; const size=512;
        const pixel=vectorDensityRasterPoint(point,bounds,size);
        const cell={x:bounds.minX+(pixel.x+.5)/size*(bounds.maxX-bounds.minX),y:bounds.minY+(pixel.y+.5)/size*(bounds.maxY-bounds.minY)};
        const view={centerX:0,centerY:1,scale:100}; const exact=screenPoint(view,point.x,point.y,800,600); const raster=screenPoint(view,cell.x,cell.y,800,600);
        console.log(JSON.stringify({distance:Math.hypot(exact.x-raster.x,exact.y-raster.y),cellSize:Math.max((bounds.maxX-bounds.minX)/size,(bounds.maxY-bounds.minY)/size)*view.scale}));
        '''
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        output = json.loads(result.stdout)
        self.assertLessEqual(output["distance"], output["cellSize"] * 1.1)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_visualization_cameras_preserve_pointer_anchors(self):
        visualizations = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app-visualizations.js").read_text(encoding="utf-8")
        self.assertNotIn("panX", visualizations)
        self.assertNotIn("panY", visualizations)
        self.assertIn("centerTime", visualizations)
        self.assertIn("densityCache", visualizations)
        self.assertIn("Math.exp(-distance", visualizations)
        start = visualizations.index("function screenPoint")
        end = visualizations.index("function visualizationMaximum", start)
        script = visualizations[start:end] + r'''
        const worldView={centerX:.2,centerY:.4,scale:300};
        const cursor={x:120,y:80}; const before=worldPoint(worldView,cursor.x,cursor.y,800,380);
        worldView.scale*=1.35;
        worldView.centerX=before.x-(cursor.x-400)/worldView.scale;
        worldView.centerY=before.y-(cursor.y-190)/worldView.scale;
        const after=screenPoint(worldView,before.x,before.y,800,380);
        const timeline={centerTime:100,visibleSpan:80}; const time=timeline.centerTime+(120-400)/800*timeline.visibleSpan;
        timeline.visibleSpan/=1.12; timeline.centerTime=time-(120-400)/800*timeline.visibleSpan;
        const timeAfter=timeline.centerTime+(120-400)/800*timeline.visibleSpan;
        console.log(JSON.stringify({world:[after.x,after.y],time:timeAfter}));
        '''
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        output = json.loads(result.stdout)
        self.assertAlmostEqual(output["world"][0], 120, places=8)
        self.assertAlmostEqual(output["world"][1], 80, places=8)
        self.assertAlmostEqual(output["time"], 72, places=8)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_stale_job_bootstrap_cannot_replace_rendered_gallery(self):
        source = javascript_source()
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
        source = javascript_source()
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
        source = javascript_source()
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
        source=javascript_source()
        html=(Path(__file__).parents[1]/"src/archive_index/web/index.html").read_text(encoding="utf-8")
        self.assertIn("async function loadSimilarPage",source)
        self.assertIn("id=\"similar-more\"",source)
        self.assertIn("similar.items = [...similar.items, ...data.items.filter",source)
        self.assertNotIn("slice(-18)",source)
        self.assertIn("similar.autoLoad",source)
        self.assertIn("Close similar assets",source)
        self.assertIn("limit = similar.initial ? 12 : 6",source)
        self.assertIn("&initial=",source)
        self.assertIn("function showToast",source)
        self.assertIn("Gallery request failed",source)
        self.assertIn("Similar assets failed",source)
        self.assertIn('id="toast-region"',html)
        self.assertIn('$("viewer").onscroll',source)
        self.assertIn('id="similarity-control" class="hidden"',html)
        self.assertIn('id="search-sort-button" class="hidden"',html)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_similar_paging_appends_all_ranked_pages(self):
        source=javascript_source()
        render=source[source.index("function renderSimilarResults"):source.index("function closeSimilar")]
        loader=source[source.index("async function loadSimilarPage"):source.index("async function showSimilar")]
        script="const escapeHtml=String;const countLabel=(count,singular,plural=`${singular}s`)=>`${count} ${count===1?singular:plural}`;"+render+loader+"""
        const section={innerHTML:'',querySelectorAll:()=>[],classList:{},scrollHeight:100,clientHeight:500,addEventListener(){}};
        const $=id=>section;const requestAnimationFrame=()=>{};const similarityMarkup=()=>'';const closeSimilar=()=>{};
        const state={viewerContext:'gallery',viewerItems:[],viewerIndex:0,similar:{assetId:'source',source:{asset_id:'source'},items:[],offset:0,total:0,strongCount:0,hasNext:true,loading:false,autoLoad:true,error:null}};
        let calls=0;const api=async()=>{const start=calls++*6;return {items:Array.from({length:6},(_,index)=>({asset_id:`asset-${start+index}`,filename:`item-${start+index}`})),total:24,strong_count:24,has_next:calls<4};};
        (async()=>{for(let i=0;i<4;i++) await loadSimilarPage();console.log(JSON.stringify({calls,items:state.similar.items.map(item=>item.filename)}));})();
        """
        completed=subprocess.run([shutil.which("node"),"--eval",script],capture_output=True,text=True,encoding="utf-8")
        self.assertEqual(completed.returncode,0,completed.stderr)
        result=json.loads(completed.stdout)
        self.assertEqual(result["calls"],4)
        self.assertEqual(result["items"],[f"item-{index}" for index in range(24)])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_similar_empty_initial_page_keeps_load_more_and_count_labels(self):
        source=javascript_source()
        render=source[source.index("function renderSimilarResults"):source.index("function closeSimilar")]
        script="const escapeHtml=String;const countLabel=(count,singular,plural=`${singular}s`)=>`${count} ${count===1?singular:plural}`;"+render+"""
        const section={innerHTML:'',querySelectorAll:()=>[],classList:{},addEventListener(){}};
        const $=id=>section;const similarityMarkup=()=>'';const closeSimilar=()=>{};const states=[];let state;
        for (const hasNext of [true,false]) {
          state={similar:{loading:false,items:[],strongCount:0,hasNext,autoLoad:false}};
          renderSimilarResults(); states.push(section.innerHTML);
        }
        console.log(JSON.stringify(states));
        """
        result=json.loads(subprocess.run([shutil.which("node"),"--eval",script],capture_output=True,text=True,encoding="utf-8",check=True).stdout)
        self.assertIn("No strongly similar assets found", result[0])
        self.assertIn('id="similar-more"', result[0])
        self.assertIn("No strongly similar assets found", result[1])
        self.assertNotIn('id="similar-more"', result[1])

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_similar_one_strong_then_weaker_page_has_no_duplicate(self):
        source=javascript_source()
        loader=source[source.index("async function loadSimilarPage"):source.index("async function showSimilar")]
        script=loader+"""
        const renderSimilarResults=()=>{};const syncSimilarViewer=()=>{};
        const state={similar:{assetId:'source',source:{asset_id:'source'},items:[],offset:0,total:0,strongCount:0,hasNext:true,initial:true,loading:false,autoLoad:false,error:null}};
        const requests=[];const api=async path=>{requests.push(path);return path.includes('initial=1')
          ? {items:[{asset_id:'strong',filename:'strong.jpg'}],total:3,strong_count:1,has_next:true}
          : {items:[{asset_id:'weak-1',filename:'weak-1.jpg'},{asset_id:'weak-2',filename:'weak-2.jpg'}],total:3,strong_count:1,has_next:false};};
        (async()=>{await loadSimilarPage();await loadSimilarPage();console.log(JSON.stringify({requests,offset:state.similar.offset,items:state.similar.items.map(item=>item.asset_id)}));})();
        """
        result=json.loads(subprocess.run([shutil.which("node"),"--eval",script],capture_output=True,text=True,encoding="utf-8",check=True).stdout)
        self.assertIn("initial=1", result["requests"][0])
        self.assertIn("offset=1", result["requests"][1])
        self.assertEqual(result["items"], ["strong", "weak-1", "weak-2"])
        self.assertEqual(result["offset"], 3)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_similar_viewer_sequence_keeps_source_at_index_zero(self):
        source=javascript_source()
        helper=source[source.index("function similarViewerItems"):source.index("function syncSimilarViewer")]
        script=helper+"""
        const state={similar:{source:{asset_id:'source'},items:[{asset_id:'source'},{asset_id:'similar-1'},{asset_id:'similar-2'}]}};
        console.log(JSON.stringify(similarViewerItems().map(item=>item.asset_id)));
        """
        result=json.loads(subprocess.run([shutil.which("node"),"--eval",script],capture_output=True,text=True,encoding="utf-8",check=True).stdout)
        self.assertEqual(result, ["source", "similar-1", "similar-2"])
        self.assertIn("showViewer(Number(button.dataset.similarIndex) + 1, similarViewerItems()", source)
        self.assertIn('showViewer(0, [source], {mode: "similar", keepSimilar:true})', source)
        self.assertIn("syncSimilarViewer();", source)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_gallery_bottom_scroll_keeps_virtual_window_at_end(self):
        source=javascript_source()
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
        source=javascript_source()
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
        source=javascript_source()
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
