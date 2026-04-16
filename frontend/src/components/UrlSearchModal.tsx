// @ts-nocheck
import { useState } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import { Ic } from "../icons";
import { Btn } from "./Btn";
import { Spin } from "./Spin";

export function UrlSearchModal({onClose,onAdded}:any){const t=useTheme();const[url,setUrl]=useState("");const[ld,setLd]=useState(false);const[data,setData]=useState<any>(null);const[err,setErr]=useState("");const[saving,setSaving]=useState(false);
const search=async()=>{if(!url.trim()){setErr("Paste a Goodreads book URL");return}setLd(true);setErr("");setData(null);try{const d=await api.post("/discovery/books/search-url",{url:url.trim()});setData(d)}catch(e){setErr("Could not fetch book details. Make sure it's a valid Goodreads URL.")}setLd(false)};
const add=async()=>{if(!data)return;setSaving(true);try{await api.post("/discovery/books/add",{title:data.title,author_name:data.author_name,series_name:data.series_name||"",series_index:data.series_index||"",pub_date:data.pub_date||"",expected_date:data.expected_date||"",description:data.description||"",isbn:data.isbn||"",cover_url:data.cover_url||"",is_unreleased:!!data.is_unreleased,source:data.source||"goodreads",source_url:data.source_url||""});onAdded&&onAdded();onClose()}catch{setErr("Failed to add book")}setSaving(false)};
return<div style={{position:"fixed",inset:0,background:"rgba(0,0,0,0.5)",zIndex:200,display:"flex",alignItems:"center",justifyContent:"center",animation:"fadeOverlay 0.2s ease-out"}} onClick={onClose}><div onClick={e=>e.stopPropagation()} className="modal-panel" style={{background:t.bg2,border:`1px solid ${t.border}`,borderRadius:12,padding:24,animation:"fadeIn 0.2s ease-out",width:500,maxWidth:"90vw",maxHeight:"85vh",overflowY:"auto",display:"flex",flexDirection:"column",gap:16}}>
<h2 style={{fontSize:18,fontWeight:700,color:t.text,margin:0}}>Add from URL</h2>
<div style={{display:"flex",gap:8}}><input value={url} onChange={e=>setUrl(e.target.value)} placeholder="https://www.goodreads.com/book/show/... or https://hardcover.app/books/..." onKeyDown={e=>e.key==="Enter"&&search()} style={{flex:1,padding:"10px 12px",background:t.inp,border:`1px solid ${t.border}`,borderRadius:8,color:t.text2,fontSize:13}}/><Btn variant="accent" onClick={search} disabled={ld}>{ld?<Spin/>:Ic.search} Fetch</Btn></div>
{err?<div style={{color:t.redt,fontSize:12}}>{err}</div>:null}
{data&&<div style={{background:t.bg3,border:`1px solid ${t.borderL}`,borderRadius:10,padding:16,display:"flex",gap:16}}>
{data.cover_url?<img src={data.cover_url} alt="" style={{width:100,height:150,objectFit:"cover",borderRadius:6,flexShrink:0}}/>:null}
<div style={{flex:1,display:"flex",flexDirection:"column",gap:6}}>
<div style={{fontSize:16,fontWeight:700,color:t.text}}>{data.title}</div>
<div style={{fontSize:13,color:t.td}}>by {data.author_name}</div>
{data.series_options?<div style={{display:"flex",alignItems:"center",gap:6,marginTop:2}}><span style={{fontSize:11,color:t.tg}}>Series:</span><select value={data.series_name||""} onChange={e=>{const picked=data.series_options.find(o=>o.name===e.target.value);setData(d=>({...d,series_name:picked?.name||"",series_index:picked?.position||""}))}} style={{padding:"2px 6px",borderRadius:4,border:`1px solid ${t.border}`,background:t.inp,color:t.purt,fontSize:12}}>{data.series_options.map(o=><option key={o.name} value={o.name}>{o.name}{o.position?` #${o.position}`:""}</option>)}<option value="">None</option></select></div>:data.series_name?<div style={{fontSize:12,color:t.purt}}>{data.series_name}{data.series_index?` #${data.series_index}`:""}</div>:null}
{data.pub_date?<div style={{fontSize:12,color:t.td}}>Published: {data.pub_date}</div>:null}
{data.expected_date?<div style={{fontSize:12,color:t.cyant}}>Expected: {data.expected_date}</div>:null}
{data.is_unreleased?<span style={{fontSize:10,fontWeight:700,background:t.cyan,color:"#fff",padding:"2px 6px",borderRadius:4,width:"fit-content"}}>UPCOMING</span>:null}
{data.isbn?<div style={{fontSize:11,color:t.tg}}>ISBN: {data.isbn}</div>:null}
{data.description?<p style={{fontSize:12,color:t.td,lineHeight:1.4,margin:0,maxHeight:100,overflow:"auto"}}>{data.description.substring(0,300)}...</p>:null}
</div></div>}
{data?<div style={{display:"flex",gap:8,justifyContent:"flex-end"}}><Btn variant="ghost" onClick={()=>{setData(null);setUrl("")}}>Clear</Btn><Btn variant="accent" onClick={add} disabled={saving}>{saving?<Spin/>:Ic.plus} Add This Book</Btn></div>:null}
{!data&&!ld?<p style={{fontSize:12,color:t.tg,textAlign:"center"}}>Paste a Goodreads book URL above and click Fetch to preview</p>:null}
</div></div>}
