import { useState, useRef } from "react";
import { useTheme } from "../theme";
import { Btn } from "./Btn";
import { Spin } from "./Spin";

export function ExportModal({onClose,defaultFilter="missing"}:any){const t=useTheme();const[filter,setFilter]=useState(defaultFilter);const[fmt,setFmt]=useState("csv");const[content,setContent]=useState<string|null>(null);const[ld,setLd]=useState(false);const[copied,setCopied]=useState(false);const[downloaded,setDownloaded]=useState(false);const taRef=useRef<HTMLTextAreaElement>(null);
const generate=async()=>{setLd(true);setCopied(false);setDownloaded(false);try{const r=await fetch(`/api/export?filter=${filter}&format=${fmt}`);const text=await r.text();setContent(text)}catch{setContent("Error generating export")}setLd(false)};
const download=()=>{if(!content)return;const blob=new Blob([content],{type:fmt==="csv"?"text/csv":"text/plain"});const url=URL.createObjectURL(blob);const a=document.createElement("a");a.href=url;a.download=`books_${filter}.${fmt==="csv"?"csv":"txt"}`;a.click();URL.revokeObjectURL(url);setDownloaded(true);setTimeout(()=>setDownloaded(false),2000)};
const copy=async()=>{if(!content)return;try{await navigator.clipboard.writeText(content);setCopied(true);setTimeout(()=>setCopied(false),2000)}catch{try{if(taRef.current){taRef.current.select();document.execCommand("copy");setCopied(true);setTimeout(()=>setCopied(false),2000)}}catch{}}};
const sel={padding:"7px 12px",borderRadius:6,border:`1px solid ${t.border}`,background:t.inp,color:t.text2,fontSize:13};
return<div style={{position:"fixed",inset:0,background:"rgba(0,0,0,0.5)",zIndex:200,display:"flex",alignItems:"center",justifyContent:"center",animation:"fadeOverlay 0.2s ease-out"}} onClick={onClose}><div onClick={e=>e.stopPropagation()} className="modal-panel" style={{background:t.bg2,border:`1px solid ${t.border}`,borderRadius:12,padding:24,animation:"fadeIn 0.2s ease-out",width:600,maxWidth:"90vw",maxHeight:"85vh",display:"flex",flexDirection:"column",gap:16}}>
<h2 style={{fontSize:18,fontWeight:700,color:t.text,margin:0}}>Export Books</h2>
<div style={{display:"flex",gap:10,alignItems:"center",flexWrap:"wrap"}}>
<div style={{display:"flex",flexDirection:"column",gap:4}}><label style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Filter</label><select value={filter} onChange={e=>{setFilter(e.target.value);setContent(null)}} style={sel}><option value="missing">Missing Only</option><option value="library">Library Only</option><option value="all">All Books</option></select></div>
<div style={{display:"flex",flexDirection:"column",gap:4}}><label style={{fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Format</label><select value={fmt} onChange={e=>{setFmt(e.target.value);setContent(null)}} style={sel}><option value="csv">CSV</option><option value="text">Text</option></select></div>
<div style={{marginTop:18}}><Btn variant="accent" onClick={generate} disabled={ld}>{ld?<Spin/>:"Generate"}</Btn></div>
</div>
{content?<>
<div style={{position:"relative"}}>
<textarea ref={taRef} readOnly value={content} style={{width:"100%",height:240,padding:12,background:t.bg3,border:`1px solid ${t.borderL}`,borderRadius:8,color:t.text2,fontSize:12,fontFamily:"monospace",resize:"vertical"}}/>
<div style={{position:"absolute",top:8,right:8,display:"flex",gap:4}}>
<Btn size="sm" onClick={copy} style={copied?{background:t.grn,borderColor:t.grn,color:"#fff"}:{}}>{copied?"✓ Copied":"Copy"}</Btn>
</div>
</div>
<div style={{display:"flex",gap:8,justifyContent:"flex-end"}}>
<Btn size="sm" onClick={download} style={downloaded?{background:t.grn,borderColor:t.grn,color:"#fff"}:{}}>{downloaded?"✓ Downloaded":<>↓ Download .{fmt==="csv"?"csv":"txt"}</>}</Btn>
<Btn size="sm" onClick={onClose} style={{background:"#6b2020",borderColor:"#8b3030",color:"#ff9090"}}>Close</Btn>
</div>
</>:null}
{!content&&!ld?<p style={{fontSize:12,color:t.tg,textAlign:"center",padding:20}}>Select filter and format, then click Generate to preview the export</p>:null}
</div></div>}
