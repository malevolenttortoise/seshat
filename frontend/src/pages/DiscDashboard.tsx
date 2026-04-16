// @ts-nocheck
import { useState, useEffect } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import { Ic } from "../icons";
import { pct, timeAgo } from "../lib/format";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { Load } from "../components/Load";
import { toast } from "../lib/toast";
import type { NavFn, Library, ScanProgress, SeriesSuggestionCountResponse } from "../types";

interface DashboardProps {
  onNav: NavFn;
  libs?: Library[];
  activeLib?: string;
  switchLib?: (slug: string) => void | Promise<void>;
}

export default function Dashboard({onNav,libs=[],activeLib="",switchLib}:DashboardProps){const t=useTheme();const[d,setD]=useState<any>(null);const[sy,setSy]=useState(false);const[scans,setScans]=useState<ScanProgress[]>([]);const[sugCount,setSugCount]=useState(0);useEffect(()=>{api.get("/discovery/stats").then(setD).catch(console.error)},[]);
// Pending-suggestions count for the Dashboard card. Refetched on the
// same `seshat:suggestions-changed` event the navbar uses, so
// Apply/Ignore actions on the SuggestionsPage immediately reflect here.
useEffect(()=>{const refresh=()=>api.get<SeriesSuggestionCountResponse>("/discovery/series-suggestions/count").then(r=>setSugCount(r.pending||0)).catch(()=>{});refresh();window.addEventListener("seshat:suggestions-changed",refresh);return()=>window.removeEventListener("seshat:suggestions-changed",refresh)},[]);
// The unified scan poller lives in App.jsx and dispatches
// `seshat:scans-updated` whenever it ticks, so the Dashboard
// just listens for that event instead of running its own polling
// loop. We also refresh `/stats` whenever a scan transitions
// running→idle (detected here by diffing the new array against the
// previous render's array).
const lookupRunning=scans.some(s=>s.kind==="lookup"&&s.running);
useEffect(()=>{const onUpdate=(e)=>{const next=(e.detail&&e.detail.scans)||[];setScans(prev=>{const someJustFinished=prev.some(p=>p.running)&&!next.some(s=>s.running);if(someJustFinished)api.get("/discovery/stats").then(setD).catch(()=>{});return next})};window.addEventListener("seshat:scans-updated",onUpdate);return()=>window.removeEventListener("seshat:scans-updated",onUpdate)},[]);
if(!d)return<Load/>;
const p=pct(d.owned_books,d.total_books);
return<div style={{display:"flex",flexDirection:"column",gap:24}}>

{libs.length>1?<div style={{marginBottom:16,display:"flex",alignItems:"center",gap:12}}><span style={{fontSize:13,fontWeight:500,color:t.tf}}>Library:</span><select value={activeLib} onChange={e=>switchLib&&switchLib(e.target.value)} style={{padding:"7px 28px 7px 12px",borderRadius:8,border:`1px solid ${t.border}`,background:t.bg2,color:t.accent,fontSize:14,fontWeight:600,cursor:"pointer",appearance:"none",WebkitAppearance:"none",backgroundImage:`url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23888'/%3E%3C/svg%3E")`,backgroundRepeat:"no-repeat",backgroundPosition:"right 10px center"}}>{libs.map(l=><option key={l.slug} value={l.slug}>{l.content_type==="audiobook"?"🎧 ":"📖 "}{l.name}</option>)}</select></div>:null}
{/* Hero */}
<div style={{background:t.bg2,border:`1px solid ${t.border}`,borderRadius:16,padding:28}}>
<div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:20}}>
<div><h1 style={{fontSize:26,fontWeight:700,color:t.text,margin:0}}>Your Library</h1>
<p style={{fontSize:14,color:t.td,marginTop:4}}>{d.owned_books} of {d.total_books} books owned</p></div>
<div style={{textAlign:"right"}}><span style={{fontSize:32,fontWeight:700,color:p===100?t.grnt:p>75?t.ylwt:t.text}}>{p}%</span>
<div style={{fontSize:11,color:t.tg}}>complete</div></div></div>
<div style={{height:8,borderRadius:4,background:t.bg4,overflow:"hidden"}}><div style={{width:`${p}%`,height:"100%",borderRadius:4,background:p===100?t.grn:p>50?`linear-gradient(90deg,${t.grn},${t.ylw})`:t.ylw,transition:"width 0.5s"}}/></div>
</div>

{/* Stat cards */}
<div className="dash-stats" style={{display:"grid",gridTemplateColumns:"repeat(auto-fit, minmax(140px, 1fr))",gap:12}}>
{[
  {label:"Owned",value:d.owned_books,color:t.grnt,icon:"📚",nav:()=>onNav("library")},
  {label:"Missing",value:d.missing_books,color:t.ylwt,icon:"🔍",nav:()=>onNav("missing")},
  {label:"New Finds",value:d.new_books,color:t.redt,icon:"✨"},
  {label:"Authors",value:d.authors,color:t.purt,icon:"✍",nav:()=>onNav("authors")},
  {label:"Series",value:d.total_series,color:t.cyant,icon:"📖"},
  {label:"Upcoming",value:d.upcoming_books||0,color:t.cyant,icon:"📅",nav:()=>onNav("upcoming")},
  // Only render the Suggestions stat card when there's something to
  // review. Conditionally appended via spread so the grid layout
  // collapses cleanly when the count is 0.
  ...(sugCount>0?[{label:"Suggestions",value:sugCount,color:t.accent,icon:"💡",nav:()=>onNav("suggestions")}]:[]),
].map(c=><div key={c.label} onClick={c.nav} style={{background:t.bg2,border:`1px solid ${t.border}`,borderRadius:12,padding:"16px 18px",cursor:c.nav?"pointer":"default",transition:"border-color 0.2s"}}><div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}><span style={{fontSize:20}}>{c.icon}</span><span style={{fontSize:24,fontWeight:700,color:c.color}}>{c.value}</span></div><div style={{fontSize:12,color:t.td,marginTop:6}}>{c.label}</div></div>)}
</div>

{d.mam_enabled&&d.mam?<div onClick={()=>onNav("mam")} style={{background:t.bg2,border:`1px solid ${t.border}`,borderRadius:12,padding:"14px 20px",cursor:"pointer",display:"flex",alignItems:"center",gap:20,flexWrap:"wrap",transition:"border-color 0.2s"}}>
<span style={{fontSize:13,fontWeight:600,color:t.tm,textTransform:"uppercase",letterSpacing:"0.04em"}}>MAM</span>
<div style={{display:"flex",alignItems:"center",gap:6}}><span style={{fontSize:16,color:t.grnt}}>↑</span><span style={{fontSize:20,fontWeight:700,color:t.grnt}}>{d.mam.upload_candidates||0}</span><span style={{fontSize:12,color:t.td}}>Upload Candidates</span></div>
<div style={{display:"flex",alignItems:"center",gap:6}}><span style={{fontSize:16,color:t.cyant}}>↓</span><span style={{fontSize:20,fontWeight:700,color:t.cyant}}>{d.mam.available_to_download||0}</span><span style={{fontSize:12,color:t.td}}>Available on MAM</span></div>
<div style={{display:"flex",alignItems:"center",gap:6}}><span style={{fontSize:16,color:t.tg}}>∅</span><span style={{fontSize:20,fontWeight:700,color:t.tg}}>{d.mam.missing_everywhere||0}</span><span style={{fontSize:12,color:t.td}}>Missing Everywhere</span></div>
{d.mam.total_unscanned>0?<div style={{marginLeft:"auto",fontSize:12,color:t.ylwt,fontStyle:"italic"}}>{d.mam.total_unscanned} unscanned</div>:null}
</div>:null}

{/* ── Actions ──
    Three logical groupings:
      1. Quick Actions: routine, fast operations (Sync Library, Scan
         Sources, MAM Scan). The "do this often" trio.
      2. Heavy Tasks: bounded but long-running, full-rescan style
         operations (Sources Full Re-Scan, MAM Full Library Scan). Lifted
         out of the regular row and given a cautionary amber sub-box so
         users intuit "this will take a while" without a popup.
      3. Unified scan progress: shared widget that surfaces every
         active/recent scan, regardless of which trigger started it.
    External link buttons (Calibre Web/Library/Hidden) stay on the right. */}
<div style={{background:t.bg2,border:`1px solid ${t.border}`,borderRadius:12,padding:20}}>

{/* Top row: Quick Actions (left) + external links (right) */}
<div style={{display:"flex",gap:20,flexWrap:"wrap"}}>
<div style={{flex:"1 1 320px"}}>
<div style={{fontSize:12,fontWeight:600,color:t.tm,textTransform:"uppercase",letterSpacing:"0.06em",marginBottom:12}}>Quick Actions</div>
<div style={{display:"flex",gap:10,flexWrap:"wrap",alignItems:"center"}}>
{(()=>{const lookup=scans.find(s=>s.kind==="lookup");const mam=scans.find(s=>s.kind==="mam");const lookupType=lookup?.type;const mamRunning=mam?.running;return<>
<Btn variant="accent" onClick={async()=>{setSy(true);try{await api.post("/discovery/sync/library");toast.success("Library synced")}catch(e){toast.error(e.message||"Sync failed")}setSy(false);api.get("/discovery/stats").then(setD)}} disabled={sy}>{sy?<Spin/>:Ic.sync} Sync Library</Btn>
<Btn onClick={async()=>{try{const r=await api.post("/discovery/sync/lookup");if(r.error)toast.warn(r.error);else if(r.due===0)toast.info(r.message||"No authors due for scanning");else{toast.info(`Source scan started — ${r.due||0} authors`);window.dispatchEvent(new CustomEvent("seshat:scan-started"))}}catch(e){toast.error(e.message||"Scan failed to start")}}} disabled={lookupRunning}>{lookupRunning&&lookupType==="lookup"?<Spin/>:Ic.search} Scan Sources</Btn>
{d.mam_enabled&&d.mam_scanning_enabled!==false?<Btn onClick={async()=>{try{const r=await api.post("/discovery/mam/scan");if(r.error)toast.warn(r.error);else if(r.status==="complete")toast.info(r.message||"No books need scanning");else{toast.info(`MAM scan started — ${r.total||0} books`);window.dispatchEvent(new CustomEvent("seshat:scan-started"))}}catch(e){toast.error(e.message||"MAM scan failed to start")}}} disabled={mamRunning&&mam?.type!=="full_scan"}>{mamRunning&&mam?.type!=="full_scan"?<Spin/>:Ic.search} MAM Scan</Btn>:null}
</>})()}
</div>
<div style={{display:"flex",gap:16,marginTop:12,fontSize:12,color:t.tg}}>
<span>{d.last_library_sync_check?.at?`Last checked: ${timeAgo(d.last_library_sync_check.at)}${d.last_library_sync_check.synced?" (synced)":" (no changes)"}`:`Last sync: ${timeAgo(d.last_library_sync?.finished_at)}`}</span>
<span>Last lookup: {timeAgo(d.last_lookup?.finished_at)}</span>
</div>
</div>
<div style={{flex:"0 0 auto",display:"flex",flexDirection:"column",gap:6,borderLeft:`1px solid ${t.borderL}`,paddingLeft:20,justifyContent:"center"}}>
{d.calibre_web_url?<button onClick={()=>window.open(d.calibre_web_url,"_blank")} style={{display:"flex",alignItems:"center",gap:8,padding:"8px 14px",background:t.accent+"18",border:`1px solid ${t.accent}33`,borderRadius:8,cursor:"pointer",fontSize:13,fontWeight:500,color:t.accent,whiteSpace:"nowrap"}}>📖 Calibre Web <span style={{fontSize:10,opacity:0.6}}>↗</span></button>:null}
{d.calibre_url?<button onClick={()=>window.open(d.calibre_url,"_blank")} style={{display:"flex",alignItems:"center",gap:8,padding:"8px 14px",background:t.pur+"18",border:`1px solid ${t.pur}33`,borderRadius:8,cursor:"pointer",fontSize:13,fontWeight:500,color:t.purt,whiteSpace:"nowrap"}}>📚 Calibre Library <span style={{fontSize:10,opacity:0.6}}>↗</span></button>:null}
<button onClick={()=>onNav("hidden")} style={{display:"flex",alignItems:"center",gap:8,padding:"8px 14px",background:t.bg4,border:`1px solid ${t.border}`,borderRadius:8,cursor:"pointer",fontSize:13,fontWeight:500,color:t.td,whiteSpace:"nowrap"}}>{Ic.hide} Hidden ({d.hidden_books||0})</button>
</div>
</div>

{/* ── Heavy Tasks ──
    Visually recessed amber sub-box. The amber tint + ⚠ icon + bold
    border on each button signal "this is a long bounded job, click
    deliberately". No confirmation popup — the styling carries the
    cautionary weight. Buttons are full-width Btn instances styled
    inline so we can use the amber theme color without adding a new
    Btn variant. */}
<div style={{marginTop:18,padding:"14px 16px",background:t.ylw+"0c",border:`1px solid ${t.ylw}33`,borderRadius:10}}>
<div style={{display:"flex",alignItems:"baseline",justifyContent:"space-between",marginBottom:10,flexWrap:"wrap",gap:8}}>
<div style={{fontSize:12,fontWeight:700,color:t.ylwt,textTransform:"uppercase",letterSpacing:"0.06em",display:"flex",alignItems:"center",gap:8}}><span style={{fontSize:14}}>⚠</span> Heavy Tasks</div>
<div style={{fontSize:11,color:t.tg,fontStyle:"italic"}}>These run for a long time and re-process every eligible book — only kick off when you're ready.</div>
</div>
<div style={{display:"flex",gap:10,flexWrap:"wrap"}}>
{(()=>{const lookup=scans.find(s=>s.kind==="lookup");const mam=scans.find(s=>s.kind==="mam");const lookupType=lookup?.type;const mamRunningFull=mam?.running&&mam?.type==="full_scan";const heavyStyle={background:t.ylw+"22",color:t.ylwt,border:`1.5px solid ${t.ylw}77`,fontWeight:600};return<>
<Btn onClick={async()=>{try{const r=await api.post("/discovery/sync/full-rescan");if(r.error)toast.warn(r.error);else{toast.info("Sources full re-scan started — this will take a while");window.dispatchEvent(new CustomEvent("seshat:scan-started"))}}catch(e){toast.error(e.message||"Full re-scan failed to start")}}} disabled={lookupRunning} style={heavyStyle}>{lookupRunning&&lookupType==="full_rescan"?<Spin/>:Ic.refresh} Sources Full Re-Scan</Btn>
{d.mam_enabled&&d.mam_scanning_enabled!==false?<Btn onClick={async()=>{try{const r=await api.post("/discovery/mam/full-scan");if(r.error)toast.warn(r.error);else{toast.info(`MAM Full Library Scan started — ${r.total_books||0} books, runs over multiple batches`);window.dispatchEvent(new CustomEvent("seshat:scan-started"))}}catch(e){toast.error(e.message||"MAM full scan failed to start")}}} disabled={mamRunningFull} style={heavyStyle}>{mamRunningFull?<Spin/>:Ic.refresh} MAM Full Library Scan</Btn>:null}
</>})()}
</div>
</div>

{/* ── Unified scan progress ── One row per active or recently
     completed scan, regardless of where it was triggered. Rows are
     keyed by scan kind so author lookup + MAM + Calibre sync can
     all show side-by-side when running concurrently. The widget
     auto-hides entirely when nothing has run yet. Per-row Stop
     buttons route cancellation to the right kind-specific endpoint
     (Calibre sync has no Stop because there's no cancel endpoint
     to call). */}
{scans.filter(scan=>scan.running||!scan.completed_at||(Date.now()/1000-scan.completed_at)<120).map(scan=>{const isLookup=scan.kind==="lookup";const isMam=scan.kind==="mam";const isLibrary=scan.kind==="library";const ex=scan.extra||{};const pctVal=scan.total>0?Math.round((scan.current/scan.total)*100):0;const cancelEndpoint=isLookup?"/lookup/cancel":scan.type==="full_scan"?"/mam/full-scan/cancel":"/mam/scan/cancel";return<div key={scan.kind} style={{marginTop:12,background:t.bg4,borderRadius:8,padding:"10px 14px"}}>{scan.running?<div>
<div style={{display:"flex",justifyContent:"space-between",fontSize:12,color:t.td,marginBottom:6}}>
<span><b style={{color:t.text2}}>{scan.label}</b>{scan.status==="paused"?" — Paused, resuming soon":scan.status==="waiting (calibre sync running)"?" — Waiting for Calibre sync...":scan.current_label?` — ${scan.current_label}${scan.current_book?` · ${scan.current_book}`:""}`:scan.current_book?` — ${scan.current_book}`:""}</span>
<span style={{fontSize:11,color:t.tg}}>{scan.current} of {scan.total} {isLookup?"authors":"books"}{isMam&&ex.remaining?(()=>{const rem=ex.remaining-(scan.current||0);return rem>0?` (${rem.toLocaleString()} total remaining)`:""})():""}</span></div>
<div style={{height:6,borderRadius:3,background:t.bg,overflow:"hidden",marginBottom:6}}><div style={{width:`${pctVal}%`,height:"100%",borderRadius:3,background:scan.status==="paused"?t.ylw:t.accent,transition:"width 0.5s"}}/></div>
<div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}>
{isLookup?<span style={{fontSize:11,color:t.tg}}>New books found: <b style={{color:t.grnt}}>{ex.new_books||0}</b></span>:isLibrary?<span style={{fontSize:11,color:t.tg}}>New: <b style={{color:t.grnt}}>{ex.books_new||0}</b> · Updated: <b style={{color:t.text2}}>{ex.books_updated||0}</b></span>:<div style={{display:"flex",gap:12,fontSize:11,color:t.tg}}><span style={{color:t.grnt}}>Found: {ex.found||0}</span><span style={{color:t.ylwt}}>Possible: {ex.possible||0}</span><span style={{color:t.redt}}>Not found: {ex.not_found||0}</span>{(ex.errors||0)>0?<span style={{color:t.red}}>Errors: {ex.errors}</span>:null}</div>}
{isLibrary?null:<Btn size="sm" onClick={async()=>{try{const r=await api.post(cancelEndpoint);toast.info(r.message||"Cancellation requested");window.dispatchEvent(new CustomEvent("seshat:scan-started"))}catch(e){toast.error(e.message||"Cancel failed")}}} style={{background:t.red+"22",color:t.redt,border:`1px solid ${t.red}44`,padding:"2px 8px",fontSize:11}}>Stop</Btn>}</div>
</div>:<div style={{fontSize:13,color:scan.status==="complete"?t.grnt:t.redt}}>{scan.status==="complete"?<>{scan.label} Complete — {isLookup?`${scan.current} authors checked, ${ex.new_books||0} new books found`:isLibrary?`${scan.current} books synced — ${ex.books_new||0} new, ${ex.books_updated||0} updated`:(()=>{const rem=ex.remaining!=null?ex.remaining-(scan.current||0):(scan.total||0)-(scan.current||0);return`${scan.current} scanned: ${ex.found||0} found, ${ex.possible||0} possible, ${ex.not_found||0} not found${(ex.errors||0)>0?`, ${ex.errors} errors`:""}${rem>0?` · ${rem.toLocaleString()} unscanned`:""}`})()}
{/* Source-timeout badge line: only renders for lookup scans that had
    at least one source hit its wall-clock cap. Check the container
    logs for the affected author list. */}
{isLookup&&ex.source_timeouts&&Object.keys(ex.source_timeouts).length>0?<div style={{fontSize:11,color:t.ylwt,marginTop:4}}>⚠ {Object.entries(ex.source_timeouts).map(([src,n])=>`${src} timed out for ${n} author${n===1?"":"s"}`).join(" · ")} — check logs for details</div>:null}
</>:`${scan.label}: ${scan.status}`}</div>}</div>})}

</div>

{/* Quick nav — order matches the top navbar. MAM is gated on the
    feature being enabled in /api/stats; Suggestions is gated on
    there being something to review (matching the top-nav visibility
    rule). The top-nav badge is intentionally NOT mirrored here —
    on the dashboard quick-nav grid the badge would overflow the
    button on large counts and crowd into the next cell. The
    appearance of the Suggestions button at all is enough signal. */}
<div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit, minmax(140px, 1fr))",gap:10}}>
{[
  {label:"Library",icon:"📖",pg:"library"},
  {label:"Authors",icon:"◉",pg:"authors"},
  {label:"Missing",icon:"◌",pg:"missing"},
  {label:"Upcoming",icon:"📅",pg:"upcoming"},
  ...(d.mam_enabled?[{label:"MAM",icon:"🔍",pg:"mam"}]:[]),
  ...(sugCount>0?[{label:"Suggestions",icon:"💡",pg:"suggestions"}]:[]),
  {label:"Settings",icon:"⚙",pg:"settings"},
].map(n=><button key={n.pg} onClick={()=>onNav(n.pg)} style={{background:t.bg2,border:`1px solid ${t.border}`,borderRadius:10,padding:"14px 16px",cursor:"pointer",display:"flex",alignItems:"center",justifyContent:"center",gap:10,fontSize:14,fontWeight:500,color:t.text2}}><span style={{fontSize:18}}>{n.icon}</span>{n.label}</button>)}
</div>
</div>}
