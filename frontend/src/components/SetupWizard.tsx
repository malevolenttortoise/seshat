// @ts-nocheck
import { useState, useEffect } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import { Btn } from "./Btn";
import { Spin } from "./Spin";

interface SetupWizardProps {
  onComplete: () => void;
}
export function SetupWizard({onComplete}:SetupWizardProps){
const t=useTheme();
const[step,setStep]=useState(0);
const[platform,setPlatform]=useState<any>(null);
// Step 2: Library paths
const[sources,setSources]=useState<any[]>([]);
const[srcPath,setSrcPath]=useState("");
const[srcType,setSrcType]=useState("root");
const[srcApp,setSrcApp]=useState("calibre");
const[validating,setValidating]=useState(false);
const[valResult,setValResult]=useState<any>(null);
// Step 3: Sources
const[hcKey,setHcKey]=useState("");
const[mamToken,setMamToken]=useState("");
// Step 4: Finish
const[saving,setSaving]=useState(false);
const[saveStep,setSaveStep]=useState("");
const[done,setDone]=useState(false);
const[result,setResult]=useState<any>(null);
const[error,setError]=useState<any>(null);

useEffect(()=>{api.get("/platform").then(setPlatform).catch(console.error)},[]);

// Handle skip — mark setup complete without configuring anything
const skip=async()=>{try{await api.post("/settings",{setup_complete:true});onComplete()}catch(e){console.error("Skip failed:",e)}};

// Validate a path
const validate=async()=>{if(!srcPath.trim())return;setValidating(true);setValResult(null);try{const r=await api.post("/discovery/libraries/validate-path",{path:srcPath.trim(),type:srcType,app_type:srcApp});setValResult(r)}catch{setValResult({valid:false,error:"Network error"})}setValidating(false)};

// Add a source to the list
const addSource=()=>{if(!srcPath.trim())return;setSources(s=>[...s,{path:srcPath.trim(),type:srcType,app_type:srcApp}]);setSrcPath("");setValResult(null)};

// Remove a source from the list
const removeSource=i=>setSources(s=>s.filter((_,idx)=>idx!==i));

// Final save + sync
const finish=async()=>{setSaving(true);setError(null);try{
// Step 1: Save settings
setSaveStep("Saving configuration...");
const settings:any={setup_complete:true};
if(sources.length>0)settings.library_sources=sources;
if(hcKey.trim())settings.hardcover_api_key=hcKey.trim();
if(mamToken.trim()){settings.mam_session_id=mamToken.trim();settings.mam_enabled=true}
await api.post("/settings",settings);
// Step 2: Rescan libraries
setSaveStep("Discovering libraries...");
const rescan=await api.post("/discovery/libraries/rescan");
const libCount=rescan.libraries?.length||0;
// Step 3: Sync if libraries found
if(libCount>0){setSaveStep("Syncing library data...");try{await api.post("/discovery/sync/library")}catch(e){console.warn("Initial sync warning:",e)}}
setResult({libraries:libCount,synced:libCount>0});setDone(true)
}catch(e){setError(String(e));setDone(true)}setSaving(false)};

// Input style helper
const ist={padding:"10px 14px",borderRadius:8,border:`1px solid ${t.border}`,background:t.inp,color:t.text2,fontSize:14,outline:"none",width:"100%"};

// Step indicator
const steps=["Welcome","Library","Sources","Finish"];
const StepDots=()=><div style={{display:"flex",justifyContent:"center",gap:8,marginBottom:32}}>{steps.map((s,i)=><div key={i} style={{display:"flex",alignItems:"center",gap:8}}><div style={{width:28,height:28,borderRadius:"50%",display:"flex",alignItems:"center",justifyContent:"center",fontSize:12,fontWeight:600,background:i===step?t.accent:i<step?t.grn:t.bg4,color:i<=step?"#000":t.tg,transition:"all 0.2s"}}>{i<step?"✓":i+1}</div>{i<steps.length-1?<div style={{width:32,height:2,background:i<step?t.grn:t.bg4,borderRadius:1}}/>:null}</div>)}</div>;

if(!platform)return<div style={{display:"flex",justifyContent:"center",alignItems:"center",minHeight:"100vh"}}><Spin/></div>;

return<div style={{minHeight:"100vh",display:"flex",alignItems:"center",justifyContent:"center",padding:20}}>
<div style={{width:"100%",maxWidth:600,background:t.bg2,border:`1px solid ${t.border}`,borderRadius:16,padding:"40px 36px",boxShadow:"0 8px 32px rgba(0,0,0,0.3)"}}>

{/* ── Step 0: Welcome ── */}
{step===0?<div style={{textAlign:"center"}}>
<div style={{fontSize:48,marginBottom:8}}>📚</div>
<h1 style={{fontSize:28,fontWeight:700,color:t.text,marginBottom:8}}>Welcome to AthenaScout</h1>
<p style={{fontSize:15,color:t.td,marginBottom:24,lineHeight:1.6}}>Your library completionist tracker. Let's get you set up in a few quick steps.</p>
<div style={{background:t.bg4,borderRadius:10,padding:"12px 16px",marginBottom:32,display:"inline-flex",gap:16,fontSize:13,color:t.tg}}>
<span>Mode: <strong style={{color:t.text2}}>{platform.is_docker?"Docker":"Standalone"}</strong></span>
<span>OS: <strong style={{color:t.text2}}>{platform.os_type}</strong></span>
</div>
{platform.is_docker?<p style={{fontSize:13,color:t.tf,marginBottom:24,lineHeight:1.5}}>Running in Docker — make sure your Calibre library volume is mounted, then add the container-side path below.</p>:<p style={{fontSize:13,color:t.tf,marginBottom:24,lineHeight:1.5}}>Running standalone — you can point directly to any Calibre library on your filesystem.</p>}
<div style={{display:"flex",gap:12,justifyContent:"center"}}>
<Btn variant="accent" onClick={()=>setStep(1)} style={{padding:"12px 32px",fontSize:16}}>Get Started</Btn>
<Btn variant="ghost" onClick={skip}>Skip Setup</Btn>
</div>
</div>:null}

{/* ── Step 1: Library Paths ── */}
{step===1?<div>
<StepDots/>
<h2 style={{fontSize:22,fontWeight:700,color:t.text,marginBottom:6}}>Add Your Library</h2>
<p style={{fontSize:13,color:t.td,marginBottom:20}}>Point AthenaScout to your Calibre library folder. You can add more libraries later from Settings.</p>

{/* Auto-detected paths */}
{platform.existing_default_paths?.length>0?<div style={{marginBottom:16}}>
<div style={{fontSize:12,fontWeight:600,color:t.grnt,marginBottom:6}}>✓ Found Calibre library on your system:</div>
{platform.existing_default_paths.map((p,i)=><div key={i} style={{display:"flex",alignItems:"center",justifyContent:"space-between",padding:"8px 12px",background:t.grn+"12",border:`1px solid ${t.grn}33`,borderRadius:8,marginBottom:4}}>
<span style={{fontSize:13,color:t.text2}}>{p.path}</span>
<Btn size="sm" variant="accent" onClick={()=>{setSources(s=>[...s,{path:p.path,type:"root",app_type:p.app_type}])}}>Add</Btn>
</div>)}
</div>:null}

{/* Manual path input */}
<div style={{display:"flex",flexDirection:"column",gap:8,marginBottom:16}}>
<div style={{display:"flex",gap:8}}>
<input value={srcPath} onChange={e=>{setSrcPath(e.target.value);setValResult(null)}} placeholder={platform.os_type==="windows"?"C:\\Users\\You\\Calibre Library":platform.is_docker?"/calibre":"~/Calibre Library"} style={{...ist,flex:1}}/>
<select value={srcType} onChange={e=>setSrcType(e.target.value)} style={{...ist,width:"auto",minWidth:130}}>
<option value="root">Root directory</option>
<option value="direct">Direct path</option>
</select>
</div>
<div style={{display:"flex",gap:8}}>
<select value={srcApp} onChange={e=>setSrcApp(e.target.value)} style={{...ist,width:"auto"}}>
<option value="calibre">📖 Calibre (ebook)</option>
<option value="audiobookshelf" disabled>🎧 Audiobookshelf (coming soon)</option>
</select>
<Btn onClick={validate} disabled={validating||!srcPath.trim()}>{validating?"Validating...":"Validate"}</Btn>
<Btn variant="accent" onClick={addSource} disabled={!srcPath.trim()}>Add</Btn>
</div>
</div>
{valResult?<div style={{fontSize:13,padding:"6px 0",color:valResult.valid?t.grnt:t.redt}}>{valResult.valid?`✓ Found ${valResult.libraries_found} library(s): ${valResult.details.map(d=>d.name).join(", ")}`:`✗ ${valResult.error}`}</div>:null}

{/* Added sources list */}
{sources.length>0?<div style={{marginTop:12,marginBottom:16}}>
<div style={{fontSize:12,fontWeight:600,color:t.tm,marginBottom:6}}>Added Sources ({sources.length})</div>
{sources.map((s,i)=><div key={i} style={{display:"flex",alignItems:"center",justifyContent:"space-between",padding:"8px 12px",background:t.bg4,borderRadius:8,marginBottom:4,border:`1px solid ${t.borderL}`}}>
<div><span style={{fontSize:13,color:t.text2}}>{s.path}</span><span style={{fontSize:11,color:t.tg,marginLeft:8}}>({s.type})</span></div>
<button onClick={()=>removeSource(i)} style={{background:"none",border:"none",cursor:"pointer",color:t.redt,fontSize:14,padding:"0 4px"}}>✕</button>
</div>)}
</div>:null}

{sources.length===0?<p style={{fontSize:12,color:t.tg,fontStyle:"italic",marginBottom:16}}>No sources added yet. Add at least one library path to continue, or skip to configure later.</p>:null}

<div style={{display:"flex",justifyContent:"space-between",marginTop:24}}>
<Btn variant="ghost" onClick={()=>setStep(0)}>← Back</Btn>
<div style={{display:"flex",gap:8}}>
<Btn variant="ghost" onClick={skip}>Skip</Btn>
<Btn variant="accent" onClick={()=>setStep(2)}>Next →</Btn>
</div>
</div>
</div>:null}

{/* ── Step 2: Sources ── */}
{step===2?<div>
<StepDots/>
<h2 style={{fontSize:22,fontWeight:700,color:t.text,marginBottom:6}}>Metadata Sources</h2>
<p style={{fontSize:13,color:t.td,marginBottom:24}}>These are optional. AthenaScout uses Goodreads and Kobo by default (no setup needed). Add these for more comprehensive results.</p>

<div style={{display:"flex",flexDirection:"column",gap:20}}>
<div style={{background:t.bg4,borderRadius:10,padding:16}}>
<div style={{fontSize:14,fontWeight:600,color:t.text2,marginBottom:4}}>Hardcover API Key</div>
<p style={{fontSize:12,color:t.tf,marginBottom:10,lineHeight:1.5}}>Get from <span style={{color:t.accent}}>hardcover.app</span> → Account → API. Include the "Bearer " prefix. Adds book data from Hardcover's database.</p>
<input value={hcKey} onChange={e=>setHcKey(e.target.value)} placeholder="Bearer eyJ..." style={ist}/>
</div>

<div style={{background:t.bg4,borderRadius:10,padding:16}}>
<div style={{fontSize:14,fontWeight:600,color:t.text2,marginBottom:4}}>MyAnonamouse Session Token</div>
<p style={{fontSize:12,color:t.tf,marginBottom:10,lineHeight:1.5}}>Get from MAM → Preferences → Security → Generate Session. Enables MAM availability scanning. Requires an active MAM account.</p>
<input value={mamToken} onChange={e=>setMamToken(e.target.value)} placeholder="Paste session token..." style={ist}/>
</div>
</div>

<div style={{display:"flex",justifyContent:"space-between",marginTop:24}}>
<Btn variant="ghost" onClick={()=>setStep(1)}>← Back</Btn>
<Btn variant="accent" onClick={()=>{setStep(3);finish()}}>Finish Setup →</Btn>
</div>
</div>:null}

{/* ── Step 3: Finish ── */}
{step===3?<div style={{textAlign:"center"}}>
<StepDots/>
{saving?<div>
<div style={{marginBottom:16}}><Spin/></div>
<h2 style={{fontSize:22,fontWeight:700,color:t.text,marginBottom:8}}>Setting Up...</h2>
<p style={{fontSize:14,color:t.td}}>{saveStep}</p>
</div>:done?<div>
{error?<div>
<div style={{fontSize:48,marginBottom:12}}>⚠️</div>
<h2 style={{fontSize:22,fontWeight:700,color:t.text,marginBottom:8}}>Setup Hit a Snag</h2>
<p style={{fontSize:13,color:t.redt,marginBottom:16}}>{error}</p>
<p style={{fontSize:13,color:t.td,marginBottom:24}}>Don't worry — your settings were saved. You can troubleshoot from the Settings page.</p>
</div>:<div>
<div style={{fontSize:48,marginBottom:12}}>🎉</div>
<h2 style={{fontSize:22,fontWeight:700,color:t.text,marginBottom:8}}>You're All Set!</h2>
{result?.libraries>0?<p style={{fontSize:14,color:t.td,marginBottom:8}}>Found <strong style={{color:t.grnt}}>{result.libraries}</strong> library(s) and synced your collection.</p>:<p style={{fontSize:14,color:t.td,marginBottom:8}}>No libraries were discovered from the paths provided. You can update your library paths in Settings.</p>}
</div>}
<Btn variant="accent" onClick={onComplete} style={{padding:"12px 32px",fontSize:16,marginTop:16}}>Go to Dashboard →</Btn>
</div>:null}
</div>:null}

</div></div>}
