"use strict";
const data = JSON.parse(document.getElementById("review-data").textContent);
const app = document.getElementById("app");
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = value => Number.isFinite(value) ? value.toFixed(3) : "—";
const pct = value => (100 * value).toFixed(1) + "%";
const chronological = (a, b) => (a.time ?? Infinity) - (b.time ?? Infinity) || a.name.localeCompare(b.name);
const home = data.home || document.querySelector(".brand").href;
const timeLabel = value => value == null ? "Time unknown" : new Date(value * 1000).toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"});
const metric = r => data.type === "archive" ? r.archive_quality : r.quality;
const qualityLabel = data.type === "archive" ? "archive quality" : "image quality";
const pageKey = "photo-review:" + location.pathname;
const activeNav = data.type === "archive" ? 1 : data.type === "experiments" ? 2 : 0;
document.querySelectorAll("nav a").forEach((a,i)=>{if(i===activeNav){a.classList.add("active");a.setAttribute("aria-current","page");}});
let visible = [], state = {scope:data.type === "archive" ? "all" : "selected", view:"groups", sort:"quality", group:"", session:"", query:"", limit:120, groupLimit:12};
if (data.type === "archive") state.view = "rank";
try { const saved = JSON.parse(sessionStorage.getItem(pageKey)); if (saved) Object.assign(state, saved); } catch {}
function saveState(){try {sessionStorage.setItem(pageKey, JSON.stringify(state));} catch {}}
function card(r){
  return `<article class="photo-card" data-record="${r.id}"><a class="photo-frame" href="${esc(r.original)}" target="_blank" rel="noopener" aria-label="Open original ${esc(r.name)}"><img src="${esc(r.thumbnail)}" alt="${esc(r.name)}" loading="lazy" decoding="async">${r.selected?'<span class="pick-badge">SELECTED</span>':""}</a><div class="photo-meta"><div class="filename">${esc(r.name)}</div><div class="photo-secondary">${data.type==="archive"?esc(r.session):`Group ${r.group} · ${r.manual?"Manual pick":timeLabel(r.time)}`}</div><div class="score-line"><span class="score">${fmt(metric(r))}<small>${qualityLabel}</small></span>${r.selection?`<span class="score">${fmt(r.selection.selection_score)}<small>${r.selected?`pick #${r.rank}`:"next-pick score"}</small></span>`:""}<button class="info-button" data-info="${r.id}" aria-label="Score details for ${esc(r.name)}">Details ↗</button></div></div></article>`;
}
function gallery(){
  data.records.forEach((r,i)=>r.id=i);
  const archive = data.type === "archive";
  const groups = [...new Set(data.records.map(r=>r.group))].sort((a,b)=>a-b);
  const sessions = [...new Set(data.records.map(r=>r.session))].sort();
  const groupOptions = groups.map(g=>{const items=data.records.filter(r=>r.group===g);return `<option value="${g}">Group ${g} · ${items.length} photos</option>`;}).join("");
  app.innerHTML=`<main class="shell"><a class="back" href="${esc(home)}">← Back to sessions</a><div class="page-heading"><div><p class="eyebrow">${archive?"Across the indexed archive":esc(data.method)+" selection"}</p><h1>${esc(data.title)}</h1><p class="subtitle">${archive?`${data.records.length.toLocaleString()} photos from ${sessions.length} sessions. Quality uses one shared reference across the archive.`:`${data.count} selected from ${data.records.length.toLocaleString()} photos. Image quality is independent of diversity; selection scores explain the shortlist.`}</p></div>${data.manifest?`<a class="button" href="${esc(data.manifest)}">Selection JSON ↗</a>`:""}</div>
  <div class="toolbar"><div class="tools"><div class="field"><span>Show</span><div class="segments" id="scope"><button data-scope="selected">Selected</button><button data-scope="all">All photos</button></div></div><div class="field"><span>View</span><div class="segments" id="view"><button data-view="groups">${archive?"By session":"Group rows"}</button><button data-view="rank">Rank by score</button><button data-view="time">Timeline</button></div></div></div><div class="tools"><label class="field"><span>${archive?"Session":"Group"}</span><select id="group-filter" aria-label="${archive?"Session":"Group"} filter"><option value="">${archive?"All sessions":"All groups"}</option>${archive?sessions.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join(""):groupOptions}</select></label><label class="field" id="sort-field"><span>Rank by</span><select id="sort"><option value="quality">${archive?"Archive image quality":"Image quality"} ↓</option>${archive?"":'<option value="preference">Quality + aesthetics ↓</option><option value="selection">Selection order</option>'}</select></label><label class="field"><span>Find photo</span><input id="search" type="search" placeholder="Filename" aria-label="Find photo by filename"></label></div></div><div class="summary-line"><span id="result-count" role="status" aria-live="polite"></span><span id="view-note"></span></div><div id="content"></div><button class="load-more" id="load-more">Show more</button></main>`;
  document.querySelectorAll("[data-scope]").forEach(b=>b.addEventListener("click",()=>{state.scope=b.dataset.scope;reset();}));
  document.querySelectorAll("[data-view]").forEach(b=>b.addEventListener("click",()=>{state.view=b.dataset.view;reset();}));
  const filter=document.getElementById("group-filter");filter.value=archive?state.session:state.group;
  if(!filter.value){state.group="";state.session="";}
  filter.addEventListener("change",()=>{state[archive?"session":"group"]=filter.value;reset();});
  document.getElementById("sort").value=state.sort;
  document.getElementById("sort").addEventListener("change",e=>{state.sort=e.target.value;reset();});
  document.getElementById("search").value=state.query;
  document.getElementById("search").addEventListener("input",e=>{state.query=e.target.value;reset();});
  document.getElementById("load-more").addEventListener("click",()=>{state.limit+=120;state.groupLimit+=12;render();});
  render();
}
function reset(){state.limit=120;state.groupLimit=12;render();}
function render(){
  const archive=data.type==="archive";
  visible=data.records.filter(r=>(state.scope==="all"||r.selected)&&(!state.group||archive||String(r.group)===state.group)&&(!state.session||!archive||r.session===state.session)&&r.name.toLowerCase().includes(state.query.toLowerCase()));
  document.querySelectorAll("[data-scope]").forEach(b=>b.setAttribute("aria-pressed",b.dataset.scope===state.scope));
  document.querySelectorAll("[data-view]").forEach(b=>b.setAttribute("aria-pressed",b.dataset.view===state.view));
  document.getElementById("sort-field").hidden=state.view!=="rank";
  const content=document.getElementById("content"), more=document.getElementById("load-more");
  let displayed=0;
  if(state.view==="groups"){
    const map=new Map();for(const r of visible){const key=archive?r.session:r.group;if(!map.has(key))map.set(key,[]);map.get(key).push(r);}
    const groups=[...map.entries()].sort((a,b)=>archive?String(a[0]).localeCompare(String(b[0])):a[0]-b[0]);
    content.className="group-list";
    content.innerHTML=groups.slice(0,state.groupLimit).map(([key,records])=>{records.sort(archive?(a,b)=>metric(b)-metric(a)||chronological(a,b):chronological);displayed+=records.length;return `<section class="group-section"><div class="group-heading"><span class="group-label">${archive?esc(key):`Group ${key}`}</span><small>${records.length} photos · ${records.filter(r=>r.selected).length} selected${archive?"":` · ${timeLabel(records[0].time)}`}</small>${archive&&records[0].session_url?`<a href="${esc(records[0].session_url)}">Open session →</a>`:""}</div><div class="group-row">${records.map(card).join("")}</div></section>`;}).join("");
    more.hidden=groups.length<=state.groupLimit;more.textContent="Show more groups";
  }else{
    visible.sort(state.view==="time"?chronological:state.sort==="selection"?(a,b)=>(a.rank??Infinity)-(b.rank??Infinity)||(b.selection?.selection_score??-Infinity)-(a.selection?.selection_score??-Infinity)||chronological(a,b):state.sort==="preference"?(a,b)=>b.preference-a.preference||chronological(a,b):(a,b)=>metric(b)-metric(a)||chronological(a,b));
    content.className="photo-grid";content.innerHTML=visible.slice(0,state.limit).map(card).join("");displayed=Math.min(state.limit,visible.length);more.hidden=visible.length<=state.limit;more.textContent="Show 120 more photos";
  }
  if(!visible.length){content.className="empty";content.textContent="No photos match these filters. Try All photos or another group.";}
  document.getElementById("result-count").textContent=`${displayed.toLocaleString()} of ${visible.length.toLocaleString()} matching photos`;
  document.getElementById("view-note").textContent=state.view==="groups"?(archive?"Sessions by date · photos by archive quality":"Groups by first capture · photos in capture order"):state.view==="time"?"Capture order":state.sort==="selection"?"Greedy selection order, then remaining candidates":"Highest score first";
  saveState();
}
function details(id){
  const r=data.records[id], archive=data.type==="archive", c=archive?r.archive_components:r.components, s=r.selection;
  const bar=(name,v)=>`<div class="component"><span>${name}</span><div class="meter"><i style="width:${Math.max(0,Math.min(100,v*100))}%"></i></div><span class="component-value">${fmt(v)}</span></div>`;
  const contribution=(label,value)=>`<div class="contribution"><span>${label}</span><strong>${value>=0?"+":""}${fmt(value)}</strong></div>`;
  document.getElementById("info-content").innerHTML=`<div class="dialog-head"><h2 id="info-title">${esc(r.name)}</h2><button id="close-info" aria-label="Close score details">Close ×</button></div><div class="dialog-body"><div class="dialog-photo"><img class="dialog-image" src="${esc(r.thumbnail)}" alt="${esc(r.name)}"><a class="button" href="${esc(r.original)}" target="_blank" rel="noopener">Open full-resolution original ↗</a><p>${esc(r.session)}<br>${r.width} × ${r.height} · ${esc(r.camera)} · ISO ${r.iso}<br>${r.manual?"Also chosen manually":""}</p></div><div><section class="score-block"><h3>${archive?"Archive image quality":"Image quality"}<span>${fmt(metric(r))}</span></h3><p class="description">Technical quality only. No aesthetics or diversity. Focus, detail and contrast are ranks within ${archive?"all indexed photos":"this session"}; exposure measures clipping. These are estimates, not calibrated quality probabilities.</p>${bar("Focus",c.focus)}${bar("Detail",c.detail)}${bar("Contrast",c.contrast)}${bar("Exposure",c.exposure)}<p class="formula">0.65 × focus + 0.20 × detail + 0.15 × contrast − clipping penalty<br>${fmt(c.focus_contribution)} + ${fmt(c.detail_contribution)} + ${fmt(c.contrast_contribution)} − ${fmt(c.clipping_penalty)} = ${fmt(c.technical)} (clamped to 0–1)</p></section>
  <section class="score-block"><h3>Selection priority<span>${s?fmt(s.selection_score):"—"}</span></h3><p class="description">${s?(r.selected?`Recorded when chosen as pick #${r.rank}.`:`Potential next pick after the ${data.count??"session's"} selected photos.`):"This selection method does not use a composite diversity score."} Selection scores change as photos are chosen; compare selection order, not scores recorded at different steps or in different sessions.</p>${s?contribution("Quality / local preference",s.quality_contribution)+contribution(s.strategy==="coverage"?"New visual coverage":"Diversity reward",s.diversity_contribution)+contribution("Repetition penalty",-s.duplicate_penalty):""}${s?`<p class="formula">${fmt(s.quality_contribution)} + ${fmt(s.diversity_contribution)} − ${fmt(s.duplicate_penalty)} = ${fmt(s.selection_score)}</p>`:""}</section>
  <details><summary>Underlying measurements & aesthetic preference</summary><div class="raw-list">${Object.entries(r.raw).map(([key,value])=>`<span>${esc(key.replaceAll("_"," "))}</span><span>${fmt(value)}</span>`).join("")}</div><p>Session technical score: ${fmt(r.quality)}<br>${s?`Group/local preference: ${fmt(s.group_quality)}<br>`:""}${s?.group_picks_before!=null?`Earlier picks from this group: ${s.group_picks_before}<br>`:""}Quality + aesthetic preference: ${fmt(r.preference)}<br>Aesthetic blend: ${pct(r.aesthetic_weight)}${r.aesthetic_raw!=null?` · raw LAION prediction: ${fmt(r.aesthetic_raw)}`:""}</p></details></div></div>`;
  document.getElementById("close-info").addEventListener("click",()=>document.getElementById("info-dialog").close());
  document.getElementById("info-dialog").showModal();
}
document.addEventListener("click",e=>{const b=e.target.closest("[data-info]");if(b)details(Number(b.dataset.info));});
document.getElementById("info-dialog").addEventListener("click",e=>{if(e.target===e.currentTarget){const rect=e.currentTarget.getBoundingClientRect();if(e.clientX<rect.left||e.clientX>rect.right||e.clientY<rect.top||e.clientY>rect.bottom)e.currentTarget.close();}});
function dashboard(){
  app.innerHTML=`<main class="shell" id="content"><div class="page-heading"><div><p class="eyebrow">Local photography archive</p><h1>Your sessions, ready to review.</h1><p class="subtitle">${data.total.toLocaleString()} indexed photos · ${data.sessions.length} sessions. Browse each shortlist or compare image quality across the archive.</p><div class="intro-links"><a class="feature-link" href="${esc(data.report)}">Read the report ↗</a><span class="muted">·</span><a class="feature-link" href="${esc(data.readme)}">Usage instructions ↗</a></div></div></div><section class="feature-strip"><div><h2>Highest-scoring images, across every session</h2><p>One shared quality reference. Rank all photos together or browse rows by session.</p></div><a class="button" href="${esc(data.archive)}">Explore all photos →</a></section><div class="page-heading"><h2>Session shortlists</h2><a class="feature-link" href="${esc(data.demo)}">100-photo Kadıköy example →</a></div><div class="session-grid">${data.sessions.map(s=>`<a class="session-card" href="${esc(s.url)}"><div class="session-cover"><img loading="lazy" src="${esc(s.cover)}" alt="Preview of ${esc(s.name)}"></div><div class="session-body"><small>${esc(s.date)}</small><p class="session-title">${esc(s.label)}</p><div class="session-footer"><span>${s.count} selected / ${s.n.toLocaleString()} photos</span><span>Review →</span></div></div></a>`).join("")}</div></main>`;
}
if(data.type==="gallery"||data.type==="archive")gallery();
else if(data.type==="dashboard")dashboard();
else if(data.type==="experiments")experimentPage();

function experimentPage(){
  let phase="holdout", yMetric="exact", speedMode="index", active="mobilenet";
  app.innerHTML=`<main class="shell" id="content"><a class="back" href="${esc(home)}">← Back to sessions</a><div class="page-heading"><div><p class="eyebrow">Selection experiments</p><h1>Agreement, coverage & time.</h1><p class="subtitle">See what each method gains and gives up. Click a point or table row for its measurements.</p><div class="intro-links"><a class="feature-link" href="${esc(data.report)}">Experiment notes ↗</a><span class="muted">·</span><a class="feature-link" href="${esc(data.preview)}">Preview the experimental shortlist →</a></div></div></div><div class="tools"><label class="field"><span>Evaluation sessions</span><select id="phase"><option value="holdout">Validation · 9 sessions (reused)</option><option value="development">Development · 8 sessions</option></select></label><label class="field"><span>Agreement measure</span><select id="agreement"><option value="exact">Exact manual match</option><option value="near">One-to-one near match</option></select></label><label class="field"><span>Speed measure</span><select id="speed-mode"><option value="index">First image index</option><option value="cached">Read cache + select</option></select></label></div><p class="notice">The new experiments reuse already-seen sessions; they are not a new untouched test. Match and coverage are session averages at the manual selection counts. Coverage and near match use DINO similarity as a proxy. Speed is measured on 1,369 Kadıköy photos, with warm GPU and fresh feature caches; model loading and Python startup are excluded.</p><div class="legend"><span><i class="swatch" style="background:var(--blue)"></i>Original methods</span><span><i class="swatch" style="background:var(--accent)"></i>New experiments</span><span>◆ Current default · ▲ Development-chosen experiment</span></div><div class="chart-grid"><section class="chart-panel"><h2>Match versus coverage</h2><p class="chart-subtitle">Up and right: more agreement and more represented subjects</p><svg id="coverage-chart" class="scatter" role="img" aria-label="Manual match versus subject coverage"></svg></section><section class="chart-panel"><h2>Match versus time</h2><p class="chart-subtitle" id="speed-caption">Up and left: more agreement with less indexing time</p><svg id="speed-chart" class="scatter" role="img" aria-label="Manual match versus time"></svg></section></div><div class="plot-details" id="plot-details" role="status" aria-live="polite"></div><div class="table-wrap"><table><thead><tr><th>Method</th><th>Exact match</th><th>Near match</th><th>Coverage</th><th>Repetitive picks</th><th>Index time</th><th>Cache + select</th></tr></thead><tbody id="method-table"></tbody></table></div><p class="notice">Index times are shared by methods using the same feature pipeline; overlapping points are expected. Uniform/random baselines have no separately measured indexing pipeline and are omitted from the time chart. Cached timings use the same 1,369 images and 138 picks for every method, including score explanations; they exclude HTML generation. The full-resolution image read/feature extraction is skipped when the cache is reused.</p></main>`;
  const values=()=>data.sets[phase];
  const timeValue=p=>p.index_seconds==null?null:speedMode==="index"?p.index_seconds:p.cached_read_seconds+p.selection_seconds;
  const detail=name=>{active=name;const p=values().find(p=>p.name===name);if(!p)return;document.getElementById("plot-details").innerHTML=`<strong>${esc(p.label)}</strong>${p.default?' · current default':p.frozen?' · chosen on development sessions':""}<br>${pct(p.exact)} exact · ${pct(p.near)} near match · ${pct(p.coverage)} coverage · ${pct(p.duplicates)} repetitive picks${p.index_seconds!=null?` · ${p.index_seconds.toFixed(1)} s indexing · ${(p.cached_read_seconds+p.selection_seconds).toFixed(3)} s cache + selection`:" · time not separately measured"}`;document.querySelectorAll(".mark").forEach(el=>el.classList.toggle("selected",el.dataset.name===active));document.querySelectorAll("#method-table tr").forEach(el=>el.classList.toggle("highlight",el.dataset.name===active));};
  function plot(svgId,xValue,xLabel){
    const svg=document.getElementById(svgId), width=Math.max(280,svg.parentElement.clientWidth), height=360;
    svg.setAttribute("viewBox",`0 0 ${width} ${height}`);
    const points=values().filter(p=>Number.isFinite(xValue(p))), margin={left:62,right:20,top:18,bottom:57};
    if(!points.length){svg.innerHTML='<text x="25" y="70">No timing measurements available.</text>';return;}
    const xv=points.map(xValue), yv=values().map(p=>100*p[yMetric]);
    const xmin=Math.min(...xv), xmax=Math.max(...xv), ymin=Math.min(...yv), ymax=Math.max(...yv);
    const xp=Math.max((xmax-xmin)*.12,.01), yp=Math.max((ymax-ymin)*.18,1);
    const domainX=[Math.max(0,xmin-xp),xmax+xp],domainY=[Math.max(0,ymin-yp),Math.min(100,ymax+yp)];
    const sx=v=>margin.left+(v-domainX[0])/(domainX[1]-domainX[0])*(width-margin.left-margin.right);
    const sy=v=>height-margin.bottom-(v-domainY[0])/(domainY[1]-domainY[0])*(height-margin.top-margin.bottom);
    let markup=`<title>${esc(xLabel)} versus ${yMetric==="exact"?"exact manual match":"near match"}</title><desc>Each point is a selection method. Use the table below for all numerical values.</desc>`;
    for(let i=0;i<=4;i++){
      const y=domainY[0]+i*(domainY[1]-domainY[0])/4, py=sy(y);
      markup+=`<line x1="${margin.left}" x2="${width-margin.right}" y1="${py}" y2="${py}" stroke="var(--line)"/><text x="${margin.left-10}" y="${py+4}" text-anchor="end">${y.toFixed(0)}%</text>`;
      const x=domainX[0]+i*(domainX[1]-domainX[0])/4;
      markup+=`<text x="${sx(x)}" y="${height-margin.bottom+24}" text-anchor="middle">${x<1?x.toFixed(2):x.toFixed(0)}</text>`;
    }
    markup+=`<text class="axis-label" x="${(width+margin.left-margin.right)/2}" y="${height-9}" text-anchor="middle">${esc(xLabel)}</text><text class="axis-label" transform="translate(16,${(height-margin.bottom+margin.top)/2}) rotate(-90)" text-anchor="middle">${yMetric==="exact"?"Exact manual match (%)":"One-to-one near match (%)"}</text>`;
    for(const p of points){const x=sx(xValue(p)),y=sy(p[yMetric]*100),color=p.round==="Original"?"var(--blue)":"var(--accent)",title=`${p.label}: ${pct(p[yMetric])} match, ${pct(p.coverage)} coverage${p.index_seconds!=null?`, ${p.index_seconds.toFixed(1)} s indexing`:""}`;let shape=p.default?`<path d="M${x} ${y-8} L${x+8} ${y} L${x} ${y+8} L${x-8} ${y} Z"`:p.frozen?`<path d="M${x} ${y-8} L${x+8} ${y+7} L${x-8} ${y+7} Z"`:`<circle cx="${x}" cy="${y}" r="5.5"`;markup+=`${shape} class="mark${p.name===active?" selected":""}" data-name="${esc(p.name)}" fill="${color}" tabindex="0" role="button" aria-label="${esc(title)}"><title>${esc(title)}</title></${p.default||p.frozen?"path":"circle"}>`;}
    svg.innerHTML=markup;
    svg.querySelectorAll(".mark").forEach(el=>{el.addEventListener("click",()=>detail(el.dataset.name));el.addEventListener("focus",()=>detail(el.dataset.name));el.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();detail(el.dataset.name);}});});
  }
  function draw(){plot("coverage-chart",p=>100*p.coverage,"Manual subject-group coverage (%)");plot("speed-chart",timeValue,speedMode==="index"?"Index time · 1,369 images (seconds)":"Read cache + select (seconds)");document.getElementById("speed-caption").textContent=speedMode==="index"?"Up and left: more agreement with less indexing time":"Up and left: more agreement with faster repeated selection";detail(active);}
  function update(){document.getElementById("method-table").innerHTML=[...values()].sort((a,b)=>b[yMetric]-a[yMetric]).map(p=>`<tr data-name="${esc(p.name)}" tabindex="0" aria-label="Details for ${esc(p.label)}"><td>${p.default?"◆ ":p.frozen?"▲ ":""}${esc(p.label)}</td><td>${pct(p.exact)}</td><td>${pct(p.near)}</td><td>${pct(p.coverage)}</td><td>${pct(p.duplicates)}</td><td>${p.index_seconds==null?"Not measured":p.index_seconds.toFixed(1)+" s"}</td><td>${p.index_seconds==null?"Not measured":(p.cached_read_seconds+p.selection_seconds).toFixed(3)+" s"}</td></tr>`).join("");document.querySelectorAll("#method-table tr").forEach(el=>{el.addEventListener("click",()=>detail(el.dataset.name));el.addEventListener("keydown",e=>{if(e.key==="Enter")detail(el.dataset.name);});});draw();}
  document.getElementById("phase").addEventListener("change",e=>{phase=e.target.value;update();});
  document.getElementById("agreement").addEventListener("change",e=>{yMetric=e.target.value;update();});
  document.getElementById("speed-mode").addEventListener("change",e=>{speedMode=e.target.value;draw();});
  new ResizeObserver(draw).observe(document.querySelector(".chart-grid"));update();
}
