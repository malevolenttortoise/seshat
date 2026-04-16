// @ts-nocheck
// Books browse page.
//
// Reused as the renderer for "My Library", "Missing Books", and
// "Upcoming Books" — App.jsx instantiates BooksPage with different
// `apiPath` and `extraParams` props for each. The page hosts the
// shared search/sort/grouping/view-mode controls, the BookSidebar
// drawer for inspecting a single book, and the bulk-select bar for
// running scans against a chosen subset.
import { useState, useEffect, useCallback, useMemo } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import { Ic } from "../icons";
import { usePersist } from "../hooks/usePersist";
import { Btn } from "../components/Btn";
import { Load } from "../components/Load";
import { VT, type ViewMode } from "../components/VT";
import { SearchBar } from "../components/SearchBar";
import { Section } from "../components/Section";
import { BGrid, BList } from "../components/BookViews";
import { BookSidebar } from "../components/BookSidebar";
import { toast } from "../lib/toast";
import { ExportModal } from "../components/ExportModal";
import type { Book, BooksResponse, MamStatusResponse } from "../types";

// ─── Books Page (Library/Missing/Upcoming) ──────────────────
interface BooksPageProps {
  title: string;
  subtitle?: string;
  apiPath?: string;
  extraParams?: Record<string, string | number | boolean>;
  showAuthor?: boolean;
  exportFilter?: string;
}

export default function BooksPage({title,subtitle,apiPath="/books",extraParams={},showAuthor=true,exportFilter}:BooksPageProps){const t=useTheme();const[bks,setBks]=useState<Book[]>([]);const[total,setTotal]=useState(0);const[pg,setPg]=useState(1);const[ld,setLd]=useState(true);const[q,setQ]=usePersist<string>(`bp_${title}_q`,"");const[vm,setVm]=usePersist<ViewMode>(`bp_${title}_vm`,"grid");const[grp,setGrp]=usePersist<string>(`bp_${title}_grp`,"all");const[sort,setSort]=usePersist<string>(`bp_${title}_sort`,"title");const[sb,setSb]=useState<Book|null>(null);const[sbClosing,setSbClosing]=useState(false);const[allCollapsed,setAllCollapsed]=useState(false);const[showExp,setShowExp]=useState(false);
const[mamFilter,setMamFilter]=usePersist<string>(`bp_${title}_mam`,"");const[mamOn,setMamOn]=useState(false);
const[selMode,setSelMode]=useState(false);const[sel,setSel]=useState<Set<number>>(new Set());const[busy,setBusy]=useState(false);
const toggleSel=id=>setSel(p=>{const n=new Set(p);if(n.has(id))n.delete(id);else n.add(id);return n});
const selectAllVisible=()=>setSel(new Set(bks.map(b=>b.id)));
const closeSb=()=>{if(!sb)return;setSbClosing(true);setTimeout(()=>{setSb(null);setSbClosing(false)},200)};
const toggleSb=b=>{if(sb&&sb.id===b.id)closeSb();else{setSbClosing(false);setSb(b)}};
const isGrouped=grp!=="all";
const perPage=isGrouped?5000:60;
const sortParam=grp==="author"?"author":grp==="series"?"series":sort;
const load=useCallback((page:number=1,signal?:AbortSignal)=>{setLd(true);const init:Record<string,string>={search:q,sort:sortParam,per_page:String(perPage),page:String(page)};for(const[k,v]of Object.entries(extraParams))init[k]=String(v);const p=new URLSearchParams(init);if(mamFilter)p.set("mam_status",mamFilter);return api.get<BooksResponse>(`${apiPath}?${p}`,signal).then(d=>{setBks(d.books);setTotal(d.total);setPg(page);setLd(false)}).catch(e=>{if(!api.isAbort(e))setLd(false)})},[q,sortParam,apiPath,grp,mamFilter]);
useEffect(()=>{const c=new AbortController();load(1,c.signal);return()=>c.abort()},[load]);
useEffect(()=>{api.get<MamStatusResponse>("/discovery/mam/status").then(r=>setMamOn(!!r.enabled)).catch(()=>{})},[]);
const totalPages=Math.max(1,Math.ceil(total/perPage));
const onAction=async(act,id)=>{const scrollY=window.scrollY;if(act==="hide")await api.post(`/books/${id}/hide`);if(act==="dismiss")await api.post(`/books/${id}/dismiss`);if(act==="delete")await api.del(`/books/${id}`);await load(pg);setTimeout(()=>window.scrollTo(0,scrollY),100)};
const dismissable=bks.filter(b=>!!b.is_new).length;
const clearData=async(type)=>{const labels={source:"source scan",mam:"MAM scan",both:"all scan"};if(!confirm(`Clear ${labels[type]} data for ${sel.size} book(s)? ${type==="source"||type==="both"?"Discovered (non-Calibre) selected books will be DELETED.":"MAM status will be reset and books will need re-scanning."}`))return;setBusy(true);try{const r=await api.post("/discovery/books/clear-scan-data",{book_ids:[...sel],clear_source:type==="source"||type==="both",clear_mam:type==="mam"||type==="both"});if(r.error){toast.error(r.error)}else{toast.success(`Cleared ${labels[type]} data for ${sel.size} book(s)`);setSel(new Set());setSelMode(false);load(pg)}}catch(e){toast.error(e.message||"Error clearing data")}setBusy(false)};
const scanMam=async()=>{if(!confirm(`Run a MAM scan against ${sel.size} selected book(s)? This will re-scan even already-scanned books.`))return;setBusy(true);try{const r=await api.post("/discovery/books/scan-mam",{book_ids:[...sel]});if(r.error){toast.error(r.error)}else{toast.success(`MAM scan complete: ${r.scanned||0} scanned, ${r.found||0} found, ${r.possible||0} possible, ${r.not_found||0} not on MAM`);setSel(new Set());setSelMode(false);load(pg)}}catch(e){toast.error(e.message||"MAM scan failed")}setBusy(false)};
const scanSources=async()=>{if(!confirm(`Run a source-plugin scan for the unique authors of ${sel.size} selected book(s)?\n\nNote: source plugins look up by author, so this will scan the WHOLE author for each unique author in your selection — not just the selected books.`))return;setBusy(true);try{const r=await api.post("/discovery/books/scan-sources",{book_ids:[...sel]});toast.info(`Source scan started — ${r.total||0} authors. Track progress on the Dashboard.`);setSel(new Set());setSelMode(false);window.dispatchEvent(new CustomEvent("seshat:scan-started"))}catch(e){toast.error(e.message||"Source scan failed to start")}setBusy(false)};

// Memoize the expensive grouping+sort pass. The JSX on the outside is
// cheap and re-runs every render; what's costly is the forEach bucketing
// + Object.entries + localeCompare sort on thousands of books, which
// would otherwise re-run on every keystroke during search, theme change,
// etc. Scoping deps to [bks, grp] means grouping only recomputes when
// the book list or grouping mode actually change.
const groupedEntries=useMemo<[string,any[]][]|null>(()=>{
  if(grp==="author"&&bks.length>0){
    const g:Record<string,any[]>={};bks.forEach(b=>{const k=b.author_name||"Unknown";if(!g[k])g[k]=[];g[k].push(b)});
    return Object.entries(g).sort(([a],[b])=>a.localeCompare(b));
  }
  if(grp==="series"&&bks.length>0){
    const g:Record<string,any[]>={};bks.forEach(b=>{const k=b.series_name||"Standalone";if(!g[k])g[k]=[];g[k].push(b)});
    return Object.entries(g).sort(([a],[b])=>a==="Standalone"?1:b==="Standalone"?-1:a.localeCompare(b));
  }
  return null;
},[bks,grp]);
let content;
const viewProps={selMode,sel,onToggleSel:toggleSel};
if(grp==="author"&&groupedEntries){content=groupedEntries.map(([name,books])=><Section key={name} title={name} count={books.length} defaultOpen={!allCollapsed}>{vm==="list"?<BList books={books} onAction={onAction} onBookClick={toggleSb} showAuthor={false} {...viewProps}/>:<BGrid books={books} onAction={onAction} onBookClick={toggleSb} {...viewProps}/>}</Section>)}
else if(grp==="series"&&groupedEntries){content=groupedEntries.map(([name,books])=><Section key={name} title={name} count={books.length} defaultOpen={!allCollapsed}>{vm==="list"?<BList books={books} onAction={onAction} onBookClick={toggleSb} showAuthor={showAuthor} {...viewProps}/>:<BGrid books={books} onAction={onAction} onBookClick={toggleSb} {...viewProps}/>}</Section>)}
else{content=vm==="list"?<BList books={bks} onAction={onAction} onBookClick={toggleSb} showAuthor={showAuthor} {...viewProps}/>:<BGrid books={bks} onAction={onAction} onBookClick={toggleSb} {...viewProps}/>}

return<div style={{display:"flex",flexDirection:"column",gap:16}}>
{/* Sticky sub-header */}
<div className="bp-sticky" style={{position:"sticky",top:56,zIndex:40,background:t.bg+"ee",backdropFilter:"blur(8px)",padding:"12px 0",marginTop:-12}}>
<div className="bp-controls" style={{display:"flex",alignItems:"center",justifyContent:"space-between",flexWrap:"wrap",gap:8}}>
<div><h1 style={{fontSize:22,fontWeight:700,color:t.text,margin:0}}>{title}</h1><p style={{fontSize:12,color:t.tf,margin:0}}>{total} {subtitle}</p></div>
<div className="bp-right" style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap"}}>
<SearchBar value={q} onChange={v=>{setQ(v);setPg(1)}}/>
{!isGrouped&&<select value={sort} onChange={e=>{setSort(e.target.value);setPg(1)}} style={{padding:"7px 10px",borderRadius:6,border:`1px solid ${t.border}`,background:t.inp,color:t.text2,fontSize:12}}><option value="title">Sort: Title</option><option value="author">Sort: Author</option><option value="date">Sort: Date</option><option value="added">Sort: Added</option></select>}
{mamOn?<select value={mamFilter} onChange={e=>{setMamFilter(e.target.value);setPg(1)}} style={{padding:"7px 10px",borderRadius:6,border:`1px solid ${t.border}`,background:mamFilter?t.accent+"22":t.inp,color:mamFilter?t.accent:t.text2,fontSize:12}}><option value="">MAM: All</option><option value="found">MAM: Found</option><option value="possible">MAM: Possible</option><option value="not_found">MAM: Not Found</option><option value="unscanned">MAM: Unscanned</option></select>:null}
<select value={grp} onChange={e=>{setGrp(e.target.value);setPg(1)}} style={{padding:"7px 10px",borderRadius:6,border:`1px solid ${t.border}`,background:t.inp,color:t.text2,fontSize:12}}><option value="all">All</option><option value="author">Group: Author</option><option value="series">Group: Series</option></select>
{isGrouped&&<Btn size="sm" variant="ghost" onClick={()=>setAllCollapsed(!allCollapsed)}>{allCollapsed?Ic.expand:Ic.collapse} {allCollapsed?"Expand":"Collapse"} All</Btn>}
<VT mode={vm} setMode={setVm}/>
{dismissable>0?<Btn size="sm" variant="ghost" onClick={async()=>{await api.post("/discovery/books/dismiss-all");load(pg)}}>Dismiss all ({dismissable})</Btn>:null}
{exportFilter?<Btn size="sm" variant="ghost" onClick={()=>setShowExp(true)}>{Ic.book} Export</Btn>:null}
<Btn size="sm" variant={selMode?"accent":"default"} onClick={()=>{setSelMode(!selMode);if(selMode)setSel(new Set())}}>{selMode?"Cancel Select":"Select"}</Btn>
</div></div>
{selMode?<div style={{display:"flex",alignItems:"center",gap:10,padding:"10px 14px",marginTop:8,background:t.bg2,border:`1px solid ${t.border}`,borderRadius:8,flexWrap:"wrap"}}>
<span style={{fontSize:13,fontWeight:600,color:t.text2}}>{sel.size} book{sel.size===1?"":"s"} selected</span>
{sel.size>0?<>
<Btn size="sm" onClick={scanSources} disabled={busy} title="Scans the unique authors of the selected books — note that source plugins lookup by author, not by individual book" style={{background:t.grn+"22",color:t.grnt,border:`1px solid ${t.grn}44`}}>{busy?"…":""} Scan Sources</Btn>
{mamOn?<Btn size="sm" onClick={scanMam} disabled={busy} style={{background:t.accent+"22",color:t.accent,border:`1px solid ${t.accent}44`}}>{busy?"…":""} Scan MAM</Btn>:null}
<span style={{width:1,height:20,background:t.border,margin:"0 4px"}}/>
<Btn size="sm" onClick={()=>clearData("source")} disabled={busy} style={{background:t.ylw+"22",color:t.ylwt,border:`1px solid ${t.ylw}44`}}>Clear Source Data</Btn>
{mamOn?<Btn size="sm" onClick={()=>clearData("mam")} disabled={busy} style={{background:t.cyan+"22",color:t.cyant,border:`1px solid ${t.cyan}44`}}>Clear MAM Data</Btn>:null}
{mamOn?<Btn size="sm" onClick={()=>clearData("both")} disabled={busy} style={{background:t.red+"22",color:t.redt,border:`1px solid ${t.red}44`}}>Clear Both</Btn>:null}
<span style={{width:1,height:20,background:t.border,margin:"0 4px"}}/>
</>:null}
<Btn size="sm" onClick={selectAllVisible} disabled={busy}>Select All on Page</Btn>
{sel.size>0?<Btn size="sm" onClick={()=>setSel(new Set())} disabled={busy}>Deselect All</Btn>:null}
</div>:null}
</div>
{ld?<Load/>:<>{content}{!isGrouped&&totalPages>1&&<div style={{display:"flex",justifyContent:"center",gap:8,padding:20,alignItems:"center"}}><Btn size="sm" disabled={pg<=1} onClick={()=>{load(pg-1);window.scrollTo(0,0)}}>← Prev</Btn><span style={{fontSize:13,color:t.td}}>Page {pg} of {totalPages}</span><Btn size="sm" disabled={pg>=totalPages} onClick={()=>{load(pg+1);window.scrollTo(0,0)}}>Next →</Btn></div>}</>}
{sb&&<BookSidebar book={sb} closing={sbClosing} onClose={closeSb} onAction={onAction} onEdit={()=>load(pg)}/>}
{showExp?<ExportModal onClose={()=>setShowExp(false)} defaultFilter={exportFilter}/>:null}
</div>}
