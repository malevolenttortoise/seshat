// @ts-nocheck
// Authors browse page.
//
// Lists every author with at least one linked book (orphan rows are
// hidden — see routers/authors.py for the reasoning). Supports search,
// sort, has-missing filter, and a bulk-select bar that triggers source
// or MAM scans across the entire selection. Clicking an author
// navigates to AuthorDetailPage.
import { useState, useEffect } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import { usePersist } from "../hooks/usePersist";
import { Btn } from "../components/Btn";
import { Load } from "../components/Load";
import { SearchBar } from "../components/SearchBar";
import { VT, type ViewMode } from "../components/VT";
import { PB } from "../components/PB";
import { toast } from "../lib/toast";
import type { NavFn, Author, AuthorsResponse, MamStatusResponse } from "../types";

export default function AuthorsPage({onNav}:{onNav:NavFn}){const t=useTheme();const[aus,setAus]=useState<Author[]>([]);const[ld,setLd]=useState(true);const[q,setQ]=usePersist<string>("ap_q","");const[sort,setSort]=usePersist<string>("ap_sort","name");const[vm,setVm]=usePersist<ViewMode>("ap_vm","list");
const[selMode,setSelMode]=useState(false);const[sel,setSel]=useState<Set<number>>(new Set());const[clearing,setClearing]=useState(false);const[scanning,setScanning]=useState(false);const[mamOn,setMamOn]=useState(false);
const[linking,setLinking]=useState(false);
useEffect(()=>{api.get<MamStatusResponse>("/discovery/mam/status").then(r=>setMamOn(!!r.enabled)).catch(()=>{})},[]);
// Link the selected authors. With exactly 2 selected, the first is
// treated as canonical and the second as alias. With 3+, each
// non-first becomes an alias of the first — keeps the API simple
// and matches user mental model ("link THESE TO this one").
const linkAuthors=async(linkType)=>{if(sel.size<2)return;const ids=[...sel];const canonical=ids[0];const aliases=ids.slice(1);const canonicalName=(aus.find(a=>a.id===canonical)||{}).name||`#${canonical}`;const label=linkType==="co_author"?"co-author":"pen name";if(!confirm(`Link ${aliases.length} author(s) as ${label}${aliases.length>1?"s":""} of ${canonicalName}?`))return;setLinking(true);let ok=0,failed=0;for(const aliasId of aliases){try{await api.post("/discovery/authors/link-pen-names",{canonical_author_id:canonical,alias_author_id:aliasId,link_type:linkType});ok++}catch(e){failed++;console.error("link failed",e)}}setLinking(false);if(ok)toast.success(`Linked ${ok} author(s) as ${label}${ok>1?"s":""} of ${canonicalName}`);if(failed)toast.error(`${failed} link(s) failed`);setSel(new Set());setSelMode(false);reload()};
const toggleSel=id=>setSel(p=>{const n=new Set(p);if(n.has(id))n.delete(id);else n.add(id);return n});
const reload=()=>{setLd(true);api.get<AuthorsResponse>(`/discovery/authors?search=${q}&sort=${sort}`).then(d=>{setAus(d.authors||[]);setLd(false)})};
const clearData=async(type)=>{const labels={source:"source scan",mam:"MAM scan",both:"all scan"};if(!confirm(`Clear ${labels[type]} data for ${sel.size} author(s)? ${type==="source"||type==="both"?"This will DELETE all discovered (non-Calibre) books for these authors.":"MAM status will be reset and books will need re-scanning."}`))return;setClearing(true);try{await api.post("/discovery/authors/clear-scan-data",{author_ids:[...sel],clear_source:type==="source"||type==="both",clear_mam:type==="mam"||type==="both"});toast.success(`Cleared ${labels[type]} data for ${sel.size} author(s)`);setSel(new Set());setSelMode(false);reload()}catch(e){toast.error(e.message||"Error clearing data")}setClearing(false)};
const scanSources=async()=>{if(!confirm(`Run a source-plugin lookup for ${sel.size} author(s)? This may take a while.`))return;setScanning(true);try{const r=await api.post("/discovery/authors/scan-sources",{author_ids:[...sel]});toast.info(`Source scan started — ${r.total||sel.size} authors. Track progress on the Dashboard.`);setSel(new Set());setSelMode(false);window.dispatchEvent(new CustomEvent("seshat:scan-started"))}catch(e){toast.error(e.message||"Scan failed to start")}setScanning(false)};
const scanMam=async()=>{if(!confirm(`Run a MAM scan for un-scanned books across ${sel.size} author(s)? This may take a while.`))return;setScanning(true);try{const r=await api.post("/discovery/authors/scan-mam",{author_ids:[...sel]});if(r.error){toast.error(r.error)}else if(r.status==="complete"){toast.info(r.message||"No un-scanned books")}else{toast.info(`MAM scan started — ${r.total||0} books. Track progress on the Dashboard.`);window.dispatchEvent(new CustomEvent("seshat:scan-started"))};setSel(new Set());setSelMode(false)}catch(e){toast.error(e.message||"MAM scan failed")}setScanning(false)};
useEffect(()=>{const c=new AbortController();setLd(true);api.get<AuthorsResponse>(`/discovery/authors?search=${encodeURIComponent(q)}&sort=${sort}`,c.signal).then(d=>{setAus(d.authors||[]);setLd(false)}).catch(e=>{if(!api.isAbort(e))setLd(false)});return()=>c.abort()},[q,sort]);
const AuthorCard=({a}:{a:Author})=><div onClick={()=>selMode?toggleSel(a.id):onNav("author",a.id)} style={{minWidth:150,maxWidth:180,flex:"1 1 150px",background:selMode&&sel.has(a.id)?t.accent+"15":t.bg2,border:`1px solid ${selMode&&sel.has(a.id)?t.accent:t.borderL}`,borderRadius:10,padding:16,cursor:"pointer",display:"flex",flexDirection:"column",alignItems:"center",gap:8,textAlign:"center",transition:"background 0.15s, border-color 0.15s"}}>{a.image_url?<img src={a.image_url} alt="" style={{width:64,height:64,borderRadius:"50%",objectFit:"cover"}}/>:<div style={{width:64,height:64,borderRadius:"50%",background:t.bg4,display:"flex",alignItems:"center",justifyContent:"center",fontSize:24,fontWeight:700,color:t.tg}}>{a.name?.charAt(0)}</div>}<div style={{fontSize:13,fontWeight:600,color:t.text2,display:"flex",alignItems:"center",gap:4,justifyContent:"center"}}>{a.name}{(a.link_count||0)>0?<span title={`${a.link_count} linked`} style={{display:"inline-flex",padding:"0 4px",borderRadius:3,fontSize:9,fontWeight:500,background:t.purb||t.bg4,color:t.purt,border:`1px solid ${t.pur}33`}}>↔{a.link_count}</span>:null}</div><div style={{display:"flex",gap:8,fontSize:11}}><span style={{color:t.grnt}}>{a.owned_count||0}</span><span style={{color:t.tg}}>/</span><span style={{color:t.ylwt}}>{a.missing_count||0}</span></div><div style={{width:"100%"}}><PB owned={a.owned_count||0} total={a.total_books||0}/></div></div>;
const AuthorRow=({a}:{a:Author})=><div onClick={()=>selMode?toggleSel(a.id):onNav("author",a.id)} style={{display:"flex",alignItems:"center",gap:14,padding:"10px 14px",borderRadius:8,cursor:"pointer",background:selMode&&sel.has(a.id)?t.accent+"15":t.bg2,border:`1px solid ${selMode&&sel.has(a.id)?t.accent:t.borderL}`,transition:"background 0.15s, border-color 0.15s"}}>{a.image_url?<img src={a.image_url} alt="" style={{width:40,height:40,borderRadius:"50%",objectFit:"cover"}}/>:<div style={{width:40,height:40,borderRadius:"50%",background:t.bg4,display:"flex",alignItems:"center",justifyContent:"center",fontSize:16,fontWeight:700,color:t.tg}}>{a.name?.charAt(0)}</div>}<div style={{flex:1,minWidth:0}}><div style={{fontSize:14,fontWeight:600,color:t.text2,display:"flex",alignItems:"center",gap:6}}>{a.name}{(a.link_count||0)>0?<span title={`${a.link_count} linked author${(a.link_count||0)>1?"s":""}`} style={{display:"inline-flex",alignItems:"center",gap:3,padding:"1px 6px",borderRadius:4,fontSize:10,fontWeight:500,background:t.purb||t.bg4,color:t.purt,border:`1px solid ${t.pur}33`}}>↔ {a.link_count}</span>:null}</div><div style={{display:"flex",gap:12,fontSize:12,marginTop:2}}><span style={{color:t.grnt}}>{a.owned_count||0} owned</span><span style={{color:t.ylwt}}>{a.missing_count||0} missing</span><span style={{color:t.purt}}>{a.series_count||0} series</span></div></div><div style={{width:80}}><PB owned={a.owned_count||0} total={a.total_books||0}/></div></div>;
return<div style={{display:"flex",flexDirection:"column",gap:16}}>
<div className="bp-controls" style={{display:"flex",alignItems:"center",justifyContent:"space-between",flexWrap:"wrap",gap:8,position:"sticky",top:56,zIndex:20,background:t.bg+"ee",backdropFilter:"blur(8px)",padding:"12px 0",marginTop:-12}}>
<h1 style={{fontSize:22,fontWeight:700,color:t.text,margin:0}}>Authors <span style={{fontSize:14,fontWeight:400,color:t.tg}}>({aus.length})</span></h1>
<div className="bp-right" style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap"}}><SearchBar value={q} onChange={setQ}/><select value={sort} onChange={e=>setSort(e.target.value)} style={{padding:"7px 10px",borderRadius:6,border:`1px solid ${t.border}`,background:t.inp,color:t.text2,fontSize:12}}><option value="name">Name</option><option value="books">Books</option><option value="missing">Missing</option></select><VT mode={vm} setMode={setVm}/><Btn size="sm" variant={selMode?"accent":"default"} onClick={()=>{setSelMode(!selMode);if(selMode)setSel(new Set())}}>{selMode?"Cancel Select":`Select`}</Btn></div></div>

{selMode&&sel.size>0?<div style={{display:"flex",alignItems:"center",gap:10,padding:"10px 14px",background:t.bg2,border:`1px solid ${t.border}`,borderRadius:8,flexWrap:"wrap"}}>
<span style={{fontSize:13,fontWeight:600,color:t.text2}}>{sel.size} author{sel.size>1?"s":""} selected</span>
<Btn size="sm" onClick={scanSources} disabled={scanning||clearing||linking} style={{background:t.grn+"22",color:t.grnt,border:`1px solid ${t.grn}44`}}>{scanning?"…":""} Scan Sources</Btn>
{mamOn?<Btn size="sm" onClick={scanMam} disabled={scanning||clearing||linking} style={{background:t.accent+"22",color:t.accent,border:`1px solid ${t.accent}44`}}>{scanning?"…":""} Scan MAM</Btn>:null}
{sel.size>=2?<><span style={{width:1,height:20,background:t.border,margin:"0 4px"}}/>
{/* Link buttons: with 2+ selected, the FIRST author becomes
    the canonical identity and the rest become aliases. The
    backend treats both link types identically — only the chip
    label differs. Useful for J.N. Chaney / Christopher Hopper
    co-author chains as well as Arand ↔ Darren pen names. */}
<Btn size="sm" onClick={()=>linkAuthors("pen_name")} disabled={linking||scanning||clearing} title={`Link as pen names of ${(aus.find(a=>a.id===[...sel][0])||{}).name||""}`} style={{background:(t.purb||t.bg4),color:t.purt,border:`1px solid ${t.pur}44`}}>{linking?"…":""} Link as Pen Names</Btn>
<Btn size="sm" onClick={()=>linkAuthors("co_author")} disabled={linking||scanning||clearing} title="Link as co-authors (treats them as one identity for scans)" style={{background:t.cyan?t.cyan+"22":t.bg4,color:t.cyant||t.text2,border:`1px solid ${(t.cyan||t.tf)}44`}}>{linking?"…":""} Link as Co-Authors</Btn></>:null}
<span style={{width:1,height:20,background:t.border,margin:"0 4px"}}/>
<Btn size="sm" onClick={()=>clearData("source")} disabled={clearing||scanning||linking} style={{background:t.ylw+"22",color:t.ylwt,border:`1px solid ${t.ylw}44`}}>Clear Source Data</Btn>
{mamOn?<Btn size="sm" onClick={()=>clearData("mam")} disabled={clearing||scanning||linking} style={{background:t.cyan+"22",color:t.cyant,border:`1px solid ${t.cyan}44`}}>Clear MAM Data</Btn>:null}
{mamOn?<Btn size="sm" onClick={()=>clearData("both")} disabled={clearing||scanning||linking} style={{background:t.red+"22",color:t.redt,border:`1px solid ${t.red}44`}}>Clear Both</Btn>:null}
<Btn size="sm" onClick={()=>setSel(new Set())}>Deselect All</Btn>
</div>:null}
{ld?<Load/>:vm==="grid"?<div style={{display:"flex",flexWrap:"wrap",gap:12,alignItems:"start"}}>{aus.map(a=><AuthorCard key={a.id} a={a}/>)}</div>:<div style={{display:"flex",flexDirection:"column",gap:2}}>{aus.map(a=><AuthorRow key={a.id} a={a}/>)}</div>}
</div>}
