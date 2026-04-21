// @ts-nocheck
import { useState, useEffect } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import { Ic } from "../icons";
import { fmtDate } from "../lib/format";
import { Btn } from "./Btn";
import { Spin } from "./Spin";
import { SBRow } from "./SBRow";
import type { MamStatusResponse } from "../types";

export function BookSidebar({book,closing:parentClosing,onClose,onAction,onEdit}:any){const t=useTheme();const[mounted,setMounted]=useState(false);useEffect(()=>{requestAnimationFrame(()=>setMounted(true));return()=>setMounted(false)},[]);const[editing,setEditing]=useState(false);const[ef,setEf]=useState<any>({});const[saving,setSaving]=useState(false);const[cwUrl,setCwUrl]=useState("");const[hermeeceUrl,setHermeeceUrl]=useState("");const[mamScanning,setMamScanning]=useState(false);const[mamOn,setMamOn]=useState(false);const[suggestion,setSuggestion]=useState<any>(null);const[sugBusy,setSugBusy]=useState<any>(null);const[hermSending,setHermSending]=useState(false);
const[absUrl,setAbsUrl]=useState("");
useEffect(()=>{api.get("/discovery/settings").then(s=>{setCwUrl(s.calibre_web_url||"");setHermeeceUrl(s.hermeece_url||"");setAbsUrl(s.abs_web_url||"")}).catch(()=>{})},[]);
useEffect(()=>{api.get<MamStatusResponse>("/discovery/mam/status").then(r=>setMamOn(!!r.enabled)).catch(()=>{})},[]);
// Fetch the active series-suggestion (if any) for this book when the
// sidebar opens. The endpoint returns `{suggestion: null}` rather than
// 404 when nothing exists, so we always reach a deterministic terminal
// state without branching on HTTP status.
useEffect(()=>{if(!book?.id){setSuggestion(null);return}let cancelled=false;api.get(`/discovery/series-suggestions/by-book/${book.id}`).then(r=>{if(!cancelled)setSuggestion(r.suggestion||null)}).catch(()=>{if(!cancelled)setSuggestion(null)});return()=>{cancelled=true}},[book?.id]);
const sugAction=async(action)=>{if(!suggestion||sugBusy)return;setSugBusy(action);try{if(action==="apply")await api.post(`/discovery/series-suggestions/${suggestion.id}/apply`);else if(action==="ignore")await api.post(`/discovery/series-suggestions/${suggestion.id}/ignore`);else if(action==="delete")await api.del(`/discovery/series-suggestions/${suggestion.id}`);try{window.dispatchEvent(new CustomEvent("athenascout:suggestions-changed"))}catch{};setSuggestion(null);if(action==="apply")onEdit&&onEdit()}catch(e){alert(`${action} failed: ${e.message||e}`)}setSugBusy(null)};
const rescanMam=async()=>{if(mamScanning)return;setMamScanning(true);try{const r=await api.post("/discovery/books/scan-mam",{book_ids:[book.id]});if(r.error){alert(`MAM scan failed: ${r.error}`)}else{const res=(r.results&&r.results[0])||{};const label=res.status==="found"?"Found ✓":res.status==="possible"?`Possible (${res.match_pct||"?"}%)`:res.status==="not_found"?"Not on MAM":"Scan complete";alert(`MAM ${label}`);onEdit&&onEdit()}}catch(e){alert(`MAM scan failed: ${e.message||e}`)}setMamScanning(false)};
const sendToHermeece=async()=>{if(hermSending)return;setHermSending(true);try{const r=await api.post("/discovery/hermeece/send",{book_ids:[book.id]});if(r.sent>0){alert(`Sent to Hermeece for download!`)}else{alert(r.message||"Failed to send")}}catch(e){alert(`Send failed: ${e.message||e}`)}setHermSending(false)};
if(!book)return null;
const startEdit=()=>{setEf({title:book.title||"",description:book.description||"",pub_date:book.pub_date||"",expected_date:book.expected_date||"",isbn:book.isbn||"",series_name:book.series_name||"",series_index:book.series_index||"",is_unreleased:!!book.is_unreleased,source_url:book.source_url||"",mam_url:book.mam_url||""});setEditing(true)};
const saveEdit=async()=>{setSaving(true);try{await api.put(`/discovery/books/${book.id}`,ef);setEditing(false);onEdit&&onEdit()}catch{}setSaving(false)};
const upE=(k,v)=>setEf(p=>({...p,[k]:v}));
const ist={padding:"6px 8px",background:t.inp,border:`1px solid ${t.border}`,borderRadius:6,color:t.text2,fontSize:13,width:"100%"};

return<div style={{position:"fixed",top:0,right:0,width:420,maxWidth:"90vw",height:"100vh",background:t.bg2,borderLeft:`1px solid ${t.border}`,zIndex:100,overflowY:"auto",padding:24,display:"flex",flexDirection:"column",gap:16,boxShadow:"-4px 0 20px rgba(0,0,0,0.3)",transform:parentClosing?"translateX(100%)":mounted?"translateX(0)":"translateX(100%)",transition:"transform 0.25s ease-out"}}>
<div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",gap:12}}>
<h2 style={{fontSize:18,fontWeight:700,color:t.text,margin:0,flex:1,lineHeight:1.3}}>{editing?<input value={ef.title} onChange={e=>upE("title",e.target.value)} style={{...ist,fontSize:16,fontWeight:700}}/>:book.title}</h2>
<div className="sb-actions" style={{display:"flex",gap:8,flexShrink:0}}>{!editing&&<button onClick={startEdit} style={{background:t.bg4,border:`1px solid ${t.border}`,borderRadius:8,cursor:"pointer",color:t.tg,padding:8,minWidth:36,minHeight:36,display:"flex",alignItems:"center",justifyContent:"center"}}>{Ic.edit}</button>}<button onClick={onClose} style={{background:t.bg4,border:`1px solid ${t.border}`,borderRadius:8,cursor:"pointer",color:t.tg,padding:8,minWidth:36,minHeight:36,display:"flex",alignItems:"center",justifyContent:"center"}}>{Ic.x}</button></div></div>
{(book.cover_url||book.cover_path||book.audiobookshelf_id)?<img src={(()=>{const slugPath=book.library_slug?`/api/discovery/covers/${book.library_slug}/${book.id}`:`/api/discovery/covers/${book.id}`;if(book.owned&&(book.cover_path||book.audiobookshelf_id))return slugPath;return book.cover_url||slugPath})()} alt="" style={{width:"100%",maxHeight:300,objectFit:"contain",borderRadius:8,background:t.bg4}}/>:null}
<div style={{display:"flex",flexDirection:"column",gap:10}}>
<SBRow label="Author" value={book.author_name}/>
{book.series_name?<div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline"}}><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Series</span><span style={{fontSize:13,color:t.purt,textAlign:"right"}}>{book.series_name}{book.series_index?<span style={{color:t.td}}> (#{book.series_index}{book.mainline_total?` of ${book.mainline_total}`:""})</span>:null}</span></div>:null}
{editing?<div style={{display:"flex",flexDirection:"column",gap:6}}>
<div><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Published</span><input type="date" value={ef.pub_date} onChange={e=>upE("pub_date",e.target.value)} style={ist}/></div>
<div><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Expected Date</span><input type="date" value={ef.expected_date} onChange={e=>upE("expected_date",e.target.value)} style={ist}/></div>
<div><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>ISBN</span><input value={ef.isbn} onChange={e=>upE("isbn",e.target.value)} style={ist}/></div>
<div><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Series</span><input value={ef.series_name} onChange={e=>upE("series_name",e.target.value)} placeholder="Enter series name (or leave empty for standalone)" style={ist}/><span style={{fontSize:10,color:t.tg,marginTop:2,display:"block"}}>Type a series name to assign. Matches existing series (case-insensitive) or creates new. Clear to make standalone.</span></div>
<div><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Series #</span><input type="number" value={ef.series_index} onChange={e=>upE("series_index",e.target.value)} style={ist}/></div>
<div style={{display:"flex",alignItems:"center",gap:6}}><input type="checkbox" checked={ef.is_unreleased} onChange={e=>upE("is_unreleased",e.target.checked)}/><span style={{fontSize:12,color:t.text2}}>Unreleased</span></div>
<div><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Source URL</span><input value={ef.source_url} onChange={e=>upE("source_url",e.target.value)} placeholder="https://www.goodreads.com/book/show/..." style={ist}/></div>
<div><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>MAM URL</span><input value={ef.mam_url} onChange={e=>upE("mam_url",e.target.value)} placeholder="https://www.myanonamouse.net/t/123456" style={ist}/><span style={{fontSize:10,color:t.tg,marginTop:2,display:"block"}}>Paste a MAM torrent URL to set status to Found. Clear to reset.</span></div>
<div><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Description</span><textarea value={ef.description} onChange={e=>upE("description",e.target.value)} rows={4} style={{...ist,resize:"vertical"}}/></div>
<div style={{display:"flex",gap:6}}><Btn size="sm" variant="accent" onClick={saveEdit} disabled={saving}>{saving?<Spin/>:"Save"}</Btn><Btn size="sm" variant="ghost" onClick={()=>setEditing(false)}>Cancel</Btn></div>
</div>:<>
<SBRow label="Published" value={book.pub_date?fmtDate(book.pub_date):book.expected_date?`Expected: ${fmtDate(book.expected_date)}`:"Unknown"}/>
<SBRow label="Status" value={book.owned?"Owned":"Missing"} color={book.owned?t.grnt:t.ylwt}/>
{(()=>{const sourceLabel=book.owned?(book.source==="audiobookshelf"||book.audiobookshelf_id?"Audiobookshelf":book.source==="calibre"||book.calibre_id?"Calibre":(book.source?(book.source[0].toUpperCase()+book.source.slice(1)):"Owned")):"Unowned";return<SBRow label="Source" value={sourceLabel} color={book.owned?t.td:t.tg}/>})()}
{cwUrl&&book.owned&&book.calibre_id?<div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Calibre Web</span><a href={`${cwUrl.replace(/\/$/,"")}/book/${book.calibre_id}`} target="_blank" rel="noopener noreferrer" style={{fontSize:13,color:t.accent,textDecoration:"none",display:"flex",alignItems:"center",gap:4}}>Open in Calibre Web <span style={{fontSize:10}}>↗</span></a></div>:null}
{absUrl&&book.owned&&book.audiobookshelf_id?<div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Audiobookshelf</span><a href={`${absUrl.replace(/\/$/,"")}/item/${book.audiobookshelf_id}`} target="_blank" rel="noopener noreferrer" style={{fontSize:13,color:t.pur||t.accent,textDecoration:"none",display:"flex",alignItems:"center",gap:4}}>Open in Audiobookshelf <span style={{fontSize:10}}>↗</span></a></div>:null}
{/* Cross-format availability — the current book's work is linked
    to another format in a different library. Surface a tiny pill
    per sibling format. Non-clickable for v1; a future pass can
    wire it to swap the sidebar to the sibling book. */}
{(()=>{const sibs=book.work_siblings;if(!Array.isArray(sibs)||sibs.length===0)return null;const myType=book.content_type||(book.audiobookshelf_id?"audiobook":"ebook");const others=sibs.filter((s:any)=>s.content_type&&s.content_type!==myType);if(others.length===0)return null;const seen=new Set<string>();const unique=others.filter((s:any)=>{if(seen.has(s.content_type))return false;seen.add(s.content_type);return true});return<div style={{display:"flex",justifyContent:"space-between",alignItems:"center",flexWrap:"wrap",gap:4}}><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Also Available</span><div style={{display:"flex",gap:4}}>{unique.map((s:any)=>{const isAudio=s.content_type==="audiobook";const color=isAudio?(t.pur||t.accent):t.jade;return<span key={s.content_type} style={{display:"inline-flex",alignItems:"center",gap:4,padding:"2px 8px",borderRadius:4,fontSize:12,fontWeight:600,background:color+"22",color:color,border:`1px solid ${color}44`}}>{isAudio?"🎧":"📖"} {isAudio?"Audiobook":"Ebook"}</span>})}</div></div>})()}
{(()=>{
  const badgeColors={goodreads:{bg:"#553b1a",fg:"#e8c070",br:"#88642a"},hardcover:{bg:"#1a3355",fg:"#70a8e8",br:"#2a5588"},kobo:{bg:"#1a4533",fg:"#70e8a8",br:"#2a8855"},amazon:{bg:"#3d2e1a",fg:"#f0a83c",br:"#7a5c2a"},ibdb:{bg:"#2a1a3d",fg:"#c070e8",br:"#5a2a88"},google_books:{bg:"#1a3333",fg:"#70c8e8",br:"#2a7788"},manual:{bg:t.bg4,fg:t.td,br:t.border}};
  const order=["goodreads","hardcover","kobo","amazon","ibdb","google_books"];
  let urls={};try{urls=JSON.parse(book.source_url||"{}")}catch{if(book.source_url&&book.source_url.startsWith("http"))urls={[book.source||"unknown"]:book.source_url}}
  const entries=order.filter(k=>urls[k]).map(k=>({name:k,url:urls[k]}));
  if(entries.length===0)return null;
  return<div style={{display:"flex",justifyContent:"space-between",alignItems:"center",flexWrap:"wrap",gap:4}}><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Metadata</span><div style={{display:"flex",gap:4,flexWrap:"wrap"}}>{entries.map(e=>{const c=badgeColors[e.name]||badgeColors.manual;return<a key={e.name} href={e.url} target="_blank" rel="noopener noreferrer" style={{display:"inline-flex",alignItems:"center",gap:4,padding:"3px 10px",borderRadius:5,fontSize:12,fontWeight:600,textDecoration:"none",background:c.bg,color:c.fg,border:`1px solid ${c.br}`}}>{e.name}<span style={{fontSize:10,opacity:0.7}}>↗</span></a>})}</div></div>
})()}
{/* Inline series-suggestion card. Only renders when an active
    (pending or ignored) suggestion exists for this book.
    Apply/Ignore/Delete hit the same endpoints SuggestionsPage uses
    and dispatch the same `athenascout:suggestions-changed` event so
    the navbar badge count stays in sync. */}
{suggestion?(()=>{const isPending=suggestion.status==="pending";const sources=Array.isArray(suggestion.sources_agreeing)?suggestion.sources_agreeing:[];const fmt=(name,idx)=>name?(idx!=null?`${name} #${idx}`:name):"standalone";return<div style={{background:t.accent+"12",border:`1px solid ${t.accent}44`,borderRadius:10,padding:"12px 14px",display:"flex",flexDirection:"column",gap:8}}>
<div style={{display:"flex",alignItems:"center",gap:6}}><span style={{fontSize:14}}>💡</span><span style={{fontSize:12,fontWeight:700,color:t.accent,textTransform:"uppercase",letterSpacing:"0.06em"}}>Series Suggestion</span>{!isPending?<span style={{fontSize:10,fontWeight:600,color:t.tg,textTransform:"uppercase",padding:"1px 6px",borderRadius:4,background:t.bg4,border:`1px solid ${t.borderL}`}}>{suggestion.status}</span>:null}</div>
<div style={{fontSize:12,color:t.text2,lineHeight:1.5}}>
<span style={{color:t.tg}}>Currently:</span> <span style={{color:t.text2}}>{fmt(suggestion.current_series_name,suggestion.current_series_index)}</span><br/>
<span style={{color:t.tg}}>Suggested:</span> <span style={{color:t.accent,fontWeight:600}}>{fmt(suggestion.suggested_series_name,suggestion.suggested_series_index)}</span>
</div>
<div style={{fontSize:11,color:t.tg}}>Agreed by: {sources.join(", ")||"—"}</div>
<div style={{display:"flex",gap:6,flexWrap:"wrap"}}>
{isPending?<>
<Btn size="sm" variant="accent" onClick={()=>sugAction("apply")} disabled={!!sugBusy}>{sugBusy==="apply"?<Spin/>:<>{Ic.check} Apply</>}</Btn>
<Btn size="sm" variant="ghost" onClick={()=>sugAction("ignore")} disabled={!!sugBusy}>{sugBusy==="ignore"?<Spin/>:"Ignore"}</Btn>
</>:null}
<Btn size="sm" variant="ghost" onClick={()=>sugAction("delete")} disabled={!!sugBusy} style={{color:t.redt}}>{sugBusy==="delete"?<Spin/>:Ic.trash}</Btn>
</div>
</div>})():null}
{(mamOn||book.mam_status)?<div>
<div style={{display:"flex",justifyContent:"space-between",alignItems:"center",gap:4}}>
<span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>MAM</span>
<div style={{display:"flex",alignItems:"center",gap:6}}>
{book.mam_status==="not_found"?<a href={book.mam_url||"#"} target="_blank" rel="noopener noreferrer" title="Search MAM for this title (no match found during last scan)" style={{display:"inline-flex",alignItems:"center",gap:4,padding:"3px 10px",borderRadius:5,fontSize:12,fontWeight:600,textDecoration:"none",background:"#3a1a1a",color:t.redt,border:`1px solid #882a2a`}}>{book.owned?"Not Found (upload)":"Not Found"}<span style={{fontSize:10,opacity:0.7}}>↗</span></a>:book.mam_url?<a href={book.mam_url} target="_blank" rel="noopener noreferrer" style={{display:"inline-flex",alignItems:"center",gap:4,padding:"3px 10px",borderRadius:5,fontSize:12,fontWeight:600,textDecoration:"none",background:book.mam_status==="found"?"#1a3a1a":"#3a3a1a",color:book.mam_status==="found"?t.grnt:t.ylwt,border:`1px solid ${book.mam_status==="found"?"#2a882a":"#88882a"}`}}>{book.mam_status==="found"?"Found":"Possible"}<span style={{fontSize:10,opacity:0.7}}>↗</span></a>:<span style={{fontSize:12,color:t.tg,fontStyle:"italic"}}>Not scanned</span>}
{mamOn?<Btn size="sm" onClick={rescanMam} disabled={mamScanning} title={book.mam_status?"Re-scan this book against MAM":"Scan this book against MAM"}>{mamScanning?<Spin/>:"↻"} {book.mam_status?"Re-scan":"Scan"}</Btn>:null}
{hermeeceUrl&&book.mam_status==="found"&&!book.mam_my_snatched?<Btn size="sm" onClick={sendToHermeece} disabled={hermSending} style={{background:t.accent+"22",color:t.accent,border:`1px solid ${t.accent}44`}}>{hermSending?<Spin/>:"⬇"} Send to Hermeece</Btn>:null}
</div>
</div>
{book.mam_url&&(book.mam_formats||book.mam_has_multiple||book.mam_my_snatched)?<div style={{display:"flex",gap:8,alignItems:"center",justifyContent:"flex-end",marginTop:3,flexWrap:"wrap"}}>
{book.mam_formats?<span style={{fontSize:11,color:t.td,fontWeight:500,textTransform:"uppercase",letterSpacing:"0.03em"}}>{book.mam_formats.split(",").join(" · ")}</span>:null}
{book.mam_my_snatched?<span title="You've already snatched this torrent on MAM" style={{fontSize:11,padding:"1px 6px",borderRadius:4,background:t.grn+"22",color:t.grnt,border:`1px solid ${t.grn}44`}}>Already snatched</span>:null}
{book.mam_has_multiple?<span style={{fontSize:11,padding:"1px 6px",borderRadius:4,background:t.ylw+"22",color:t.ylwt,border:`1px solid ${t.ylw}33`}}>Multiple uploads</span>:null}
</div>:null}
</div>:null}
{book.rating?<div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline"}}><span style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Rating</span><span style={{fontSize:13,color:t.ylwt}}>{"★".repeat(Math.round(book.rating))}{"☆".repeat(5-Math.round(book.rating))} <span style={{fontSize:11,color:t.td}}>({book.rating})</span></span></div>:null}
{book.isbn?<SBRow label="ISBN" value={book.isbn}/>:null}
{book.page_count?<SBRow label="Pages" value={book.page_count}/>:null}
{book.language?<SBRow label="Language" value={book.language}/>:null}
{book.publisher?<SBRow label="Publisher" value={book.publisher}/>:null}
{book.formats?<SBRow label="Formats" value={book.formats}/>:null}
{/* ── Audiobook-specific rows ── */}
{/* Gated by `content_type === "audiobook"` OR presence of any
    audiobook-only field, so rows that arrived without a library
    badge (legacy caches) still surface narrator/duration. */}
{(book.content_type==="audiobook"||book.audiobookshelf_id||book.narrator||book.duration_sec||book.asin||book.audio_formats)?<>
  {book.narrator?<SBRow label="Narrator" value={book.narrator}/>:null}
  {book.duration_sec?<SBRow label="Duration" value={`${Math.floor(book.duration_sec/3600)}h ${Math.round((book.duration_sec%3600)/60)}m`}/>:null}
  {book.audio_formats?<SBRow label="Audio Formats" value={book.audio_formats}/>:null}
  {book.asin?<SBRow label="ASIN" value={<a href={`https://www.audible.com/pd/${book.asin}`} target="_blank" rel="noopener noreferrer" style={{color:t.accent,textDecoration:"none"}}>{book.asin} <span style={{fontSize:10,opacity:0.7}}>↗</span></a>}/>:null}
  {book.abridged?<SBRow label="Edition" value={<span style={{color:t.ylwt,fontWeight:600}}>Abridged</span>}/>:null}
</>:null}
{book.library_name?<SBRow label="Library" value={<span style={{fontSize:12,padding:"1px 8px",borderRadius:4,background:book.content_type==="audiobook"?t.purb:t.cyanb,color:book.content_type==="audiobook"?t.purt:t.cyant,border:`1px solid ${book.content_type==="audiobook"?t.pur+"33":t.cyan+"33"}`}}>{book.content_type==="audiobook"?"🎧":"📖"} {book.library_name}</span>}/>:null}
{book.tags?<div style={{marginTop:4}}><div style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase",marginBottom:4}}>Tags</div><div style={{display:"flex",flexWrap:"wrap",gap:4}}>{book.tags.split(", ").map(tag=><span key={tag} style={{padding:"2px 8px",borderRadius:4,fontSize:11,background:t.purb,color:t.purt,border:`1px solid ${t.pur}33`}}>{tag}</span>)}</div></div>:null}
{book.description?<div style={{marginTop:4}}><div style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase",marginBottom:4}}>Description</div><p style={{fontSize:13,color:t.td,lineHeight:1.5,margin:0,maxHeight:200,overflow:"auto"}}>{book.description}</p></div>:null}
</>}
</div>
{!editing&&book.hidden?<div className="sb-actions" style={{display:"flex",gap:8,marginTop:"auto",paddingTop:12,borderTop:`1px solid ${t.borderL}`,flexWrap:"wrap"}}>
<Btn size="sm" variant="accent" onClick={()=>{onAction("unhide",book.id);onClose()}}>Unhide</Btn>
</div>:!editing&&!book.owned?<div className="sb-actions" style={{display:"flex",gap:8,marginTop:"auto",paddingTop:12,borderTop:`1px solid ${t.borderL}`,flexWrap:"wrap"}}>
<Btn size="sm" onClick={()=>{onAction("dismiss",book.id);onClose()}}>Dismiss</Btn>
<Btn size="sm" onClick={()=>{onAction("hide",book.id);onClose()}}>{Ic.hide} Hide</Btn>
<Btn size="sm" onClick={()=>{if(confirm(`Delete "${book.title}" permanently? This cannot be undone.`)){onAction("delete",book.id);onClose()}}} style={{background:"#6b2020",borderColor:"#8b3030",color:"#ff9090"}}>Delete</Btn>
</div>:null}
</div>}
