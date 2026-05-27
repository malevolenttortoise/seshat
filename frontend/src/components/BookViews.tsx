// Book rendering family — card, list row, and grid/list wrappers.
// Used together by every book listing page (Library, Missing, Upcoming,
// Hidden, Author Detail, Series detail). Kept in one file because BList
// only ever wraps BListRow and BGrid only ever wraps BCard.
import { useTheme } from "../theme";
import { fmtDate } from "../lib/format";
import type { Book, BookActionHandler } from "../types";

// Shared props across the single-book renderers (BCard, BListRow).
// `onAction` is accepted but currently unused inside both renderers —
// kept in the signature so callers can pass through their existing
// action handler without two-tier prop drilling; the type matches
// `BookActionHandler` (action + book id) so BookSidebar and BList
// share one callback shape. `sel` (the Set used in the list/grid
// wrappers) is the parent's selection state and passed in per-row
// as `selected: boolean`.
interface BookViewItemProps {
  book: Book;
  onAction?: BookActionHandler;
  onClick?: (book: Book) => void;
  showAuthor?: boolean;
  // number | string because AuthorDetailPage's prop shape passes
  // whatever the router gave it (router-state integer when navigated
  // from the app, URL-string if deep-linked). The comparison against
  // `book.author_id` coerces via !== so both shapes work.
  highlightAuthorId?: number | string;
  showMamLink?: boolean;
  onSendToPipeline?: (ids: number[]) => void;
  selMode?: boolean;
  selected?: boolean;
  onToggleSel?: (id: number) => void;
  // v3.0.0 Phase 7 — when set, byline author names link to author detail.
  onAuthorClick?: (authorId: number) => void;
}

// Wrapper props for the list/grid components. The `books` array and
// `sel` Set are the only distinct fields from the item props above —
// everything else is forwarded 1:1 to each rendered BCard / BListRow.
interface BookViewListProps {
  books: Book[];
  onAction?: BookActionHandler;
  onBookClick?: (book: Book) => void;
  showAuthor?: boolean;
  highlightAuthorId?: number | string;
  showMamLink?: boolean;
  onSendToPipeline?: (ids: number[]) => void;
  selMode?: boolean;
  sel?: Set<number>;
  onToggleSel?: (id: number) => void;
  // v3.0.0 Phase 7 — forwarded to each row's byline (clickable author names).
  onAuthorClick?: (authorId: number) => void;
}

// Resolve the cover src for a book row. Books from cross-library
// aggregations carry `library_slug`; we prefer the slug-scoped
// `/api/discovery/covers/{slug}/{id}` endpoint because it reads from
// the correct library DB and (for audiobook rows) proxies ABS's
// /api/items/{id}/cover under the configured bearer token. The bare
// `/api/discovery/covers/{id}` path still works for active-library
// reads when library_slug is absent.
function coverSrcFor(book:Book):string{
  const slugPath=book.library_slug?`/api/discovery/covers/${book.library_slug}/${book.id}`:`/api/discovery/covers/${book.id}`;
  if(book.owned&&(book.cover_path||book.audiobookshelf_id))return slugPath;
  return book.cover_url||slugPath;
}

// Cross-format availability. When a book's work has a sibling in
// another content type (ebook + audiobook of the same work), we
// surface a tiny chip so the user knows "I also own this as an
// audiobook" without opening the sidebar. Returns null for books
// with no pairing.
function pairedFormat(book:any):"ebook"|"audiobook"|null{
  const sibs=book?.work_siblings;
  if(!Array.isArray(sibs)||sibs.length===0)return null;
  const myType=book.content_type||(book.audiobookshelf_id?"audiobook":"ebook");
  const other=sibs.find((s:any)=>s.content_type&&s.content_type!==myType);
  return other?other.content_type:null;
}

// v3.0.0 Phase 7 — multi-author byline. Renders the first two contributors
// in position order (primary first), then "+N more" with a tooltip listing
// the full set. Names are clickable through to the author detail page when
// `onAuthorClick` is supplied (surfaces without nav access omit it → plain
// text). Falls back to the single `author_name` when the backend didn't
// stamp a `contributors` array (older payloads / non-flipped endpoints).
function renderByline(book:Book,tg:string,onAuthorClick?:(authorId:number)=>void){
  const contribs=book.contributors;
  if(!contribs||contribs.length===0)return book.author_name||null;
  const shown=contribs.slice(0,2);
  const extra=contribs.length-shown.length;
  const all=contribs.map(c=>c.name).join(", ");
  return <span title={extra>0?all:undefined}>{shown.map((c,i)=><span key={c.author_id}>{i>0?", ":""}{onAuthorClick?<span onClick={e=>{e.stopPropagation();onAuthorClick(c.author_id)}} style={{cursor:"pointer",textDecoration:"underline",textDecorationStyle:"dotted",textUnderlineOffset:2}}>{c.name}</span>:c.name}</span>)}{extra>0?<span style={{color:tg}}>{` +${extra} more`}</span>:null}</span>;
}

export function BCard({book,onAction,onClick,showAuthor,highlightAuthorId,showMamLink,onSendToPipeline,selMode,selected,onToggleSel,onAuthorClick}:BookViewItemProps){const t=useTheme();const isUp=!!book.is_unreleased;const hasCover=book.cover_url||book.cover_path||book.audiobookshelf_id;const isOtherAuthor=highlightAuthorId&&book.author_id&&book.author_id!==highlightAuthorId;const paired=pairedFormat(book);
const handleClick=()=>{if(selMode){onToggleSel&&onToggleSel(book.id)}else{onClick&&onClick(book)}};
return<div className="bcard" onClick={handleClick} style={{minWidth:160,maxWidth:200,flex:"1 1 160px",background:selMode&&selected?t.accent+"15":t.bg2,border:`1px solid ${selMode&&selected?t.accent:isUp?t.cyan+"66":t.border}`,borderRadius:10,overflow:"hidden",cursor:"pointer",transition:"border-color 0.2s, background 0.15s, transform 0.18s ease-out, box-shadow 0.18s ease-out",position:"relative"}}>{isUp?<span style={{position:"absolute",top:6,left:6,fontSize:9,fontWeight:700,background:t.cyan,color:"#fff",padding:"2px 6px",borderRadius:4,zIndex:2}}>UPCOMING</span>:null}{paired?<span title={`Also available as ${paired}`} style={{position:"absolute",top:6,left:isUp?undefined:6,right:isUp?6:undefined,fontSize:12,background:(paired==="audiobook"?t.pur:t.jade)+"cc",color:"#fff",padding:"2px 6px",borderRadius:4,zIndex:2,lineHeight:1}}>{paired==="audiobook"?"🎧":"📖"}</span>:null}{book.is_new?<span style={{position:"absolute",top:6,right:paired&&!isUp?32:6,fontSize:9,fontWeight:700,background:t.red,color:"#fff",padding:"2px 6px",borderRadius:4,zIndex:2}}>NEW</span>:null}{book.owned===1&&!book.is_new?<span style={{position:"absolute",top:6,right:paired&&!isUp?32:6,fontSize:9,fontWeight:600,background:t.grn,color:"#fff",padding:"2px 6px",borderRadius:4,zIndex:2}}>OWNED</span>:null}{!book.owned&&!book.is_new&&!isUp?<span style={{position:"absolute",top:6,right:paired&&!isUp?32:6,fontSize:9,fontWeight:700,background:"rgba(0,0,0,0.82)",color:t.redt||t.red,padding:"2px 6px",borderRadius:4,zIndex:2,border:`1px solid ${t.red}`,letterSpacing:"0.04em"}}>MISSING</span>:null}{isOtherAuthor?<span title="From a shared series (different author)" style={{position:"absolute",top:isUp?28:6,left:6,fontSize:9,fontWeight:700,background:t.cyan,color:"#fff",padding:"2px 6px",borderRadius:4,zIndex:2}}>SHARED</span>:null}
<div style={{height:200,background:t.bg3,display:"flex",alignItems:"center",justifyContent:"center",overflow:"hidden",opacity:isUp?0.7:1}}>{hasCover?<img src={coverSrcFor(book)} alt="" className="bcard-cover" loading="lazy" style={{width:"100%",height:"100%",objectFit:"cover",opacity:0,transition:"opacity 0.35s ease-out"}} onLoad={(e:any)=>{e.currentTarget.style.opacity="1"}} onError={(e:any)=>{e.target.style.display="none";e.target.nextSibling.style.display="flex"}}/>:null}<div style={{display:hasCover?"none":"flex",flexDirection:"column",alignItems:"center",gap:4,color:t.tg,fontSize:12,textAlign:"center",padding:12}}><span style={{fontSize:28}}>?</span><span>{book.title}</span></div></div>
<div style={{padding:"8px 10px"}}><div style={{fontSize:13,fontWeight:600,color:t.text2,lineHeight:1.3,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{book.title}</div>{book.series_name&&book.series_index?<div style={{fontSize:10,color:t.purt,marginTop:1}}>#{book.series_index}{book.mainline_total?` of ${book.mainline_total}`:""}</div>:null}{showAuthor&&(book.contributors?.length||book.author_name)?<div style={{fontSize:11,color:isOtherAuthor?t.ylwt:t.td,marginTop:2,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{renderByline(book,t.tg,onAuthorClick)}</div>:null}{isUp&&book.expected_date?<div style={{fontSize:11,color:t.cyant,marginTop:2}}>Expected: {fmtDate(book.expected_date)}</div>:null}{showMamLink&&book.mam_url?(()=>{const isF=book.mam_status==="found";const isNF=book.mam_status==="not_found";const fg=isF?t.grnt:isNF?t.redt:t.ylwt;const bg=isF?"#1a3a1a":isNF?"#3a1a1a":"#3a3a1a";const br=isF?"#2a882a33":isNF?"#882a2a33":"#88882a33";return<div style={{display:"flex",alignItems:"center",gap:3,marginTop:3}}><a href={book.mam_url} target="_blank" rel="noopener noreferrer" onClick={e=>e.stopPropagation()} style={{display:"inline-flex",alignItems:"center",gap:3,fontSize:10,fontWeight:600,color:fg,textDecoration:"none",padding:"2px 6px",borderRadius:4,background:bg,border:`1px solid ${br}`}}>MAM ↗</a>{onSendToPipeline&&isF&&!book.mam_my_snatched?<button onClick={e=>{e.stopPropagation();onSendToPipeline([book.id])}} style={{fontSize:9,fontWeight:600,color:t.purt,background:t.pur+"22",border:`1px solid ${t.pur}44`,borderRadius:4,padding:"2px 5px",cursor:"pointer"}}>⬇</button>:null}</div>})():null}</div></div>}

export function BListRow({book,onAction,onClick,showAuthor,highlightAuthorId,showMamLink,onSendToPipeline,selMode,selected,onToggleSel,onAuthorClick}:BookViewItemProps){const t=useTheme();const isOtherAuthor=highlightAuthorId&&book.author_id&&book.author_id!==highlightAuthorId;const handleClick=()=>{if(selMode){onToggleSel&&onToggleSel(book.id)}else{onClick&&onClick(book)}};const paired=pairedFormat(book);return<tr onClick={handleClick} style={{cursor:"pointer",borderBottom:`1px solid ${t.borderL}`,background:selMode&&selected?t.accent+"15":"transparent"}}><td style={{padding:"8px 12px",fontSize:13,color:t.text2}}>{book.title}{paired?<span title={`Also available as ${paired}`} style={{marginLeft:8,fontSize:12,lineHeight:1}}>{paired==="audiobook"?"🎧":"📖"}</span>:null}{book.is_new?<span style={{marginLeft:8,fontSize:9,fontWeight:700,background:t.red,color:"#fff",padding:"1px 5px",borderRadius:3}}>NEW</span>:null}{book.is_unreleased?<span style={{marginLeft:8,fontSize:9,fontWeight:700,background:t.cyan,color:"#fff",padding:"1px 5px",borderRadius:3}}>UPCOMING</span>:null}</td>{showAuthor?<td style={{padding:"8px 12px",fontSize:13,color:isOtherAuthor?t.ylwt:t.td}}>{renderByline(book,t.tg,onAuthorClick)}</td>:null}<td style={{padding:"8px 12px",fontSize:13,color:t.td}}>{book.series_name?`${book.series_name}${book.series_index?` #${book.series_index}`:""}${book.mainline_total?` (${book.mainline_total})`:""}`:"—"}</td><td style={{padding:"8px 12px",fontSize:13,color:book.pub_date?t.td:book.expected_date?t.cyant:t.tg}}>{book.pub_date?fmtDate(book.pub_date):book.expected_date?fmtDate(book.expected_date):"Unknown"}</td><td style={{padding:"8px 12px",fontSize:11,color:t.tg}}>{book.source||"—"}</td>{showMamLink?<td style={{padding:"8px 12px"}}><div style={{display:"flex",alignItems:"center",gap:4}}>{book.mam_url?(()=>{const isF=book.mam_status==="found";const isNF=book.mam_status==="not_found";const fg=isF?t.grnt:isNF?t.redt:t.ylwt;const bg=isF?"#1a3a1a":isNF?"#3a1a1a":"#3a3a1a";const br=isF?"#2a882a33":isNF?"#882a2a33":"#88882a33";return<a href={book.mam_url} target="_blank" rel="noopener noreferrer" onClick={e=>e.stopPropagation()} style={{fontSize:11,fontWeight:600,color:fg,textDecoration:"none",padding:"2px 8px",borderRadius:4,background:bg,border:`1px solid ${br}`}}>MAM ↗</a>})():<span style={{fontSize:11,color:t.tg}}>—</span>}{onSendToPipeline&&book.mam_status==="found"&&!book.mam_my_snatched?<button onClick={e=>{e.stopPropagation();onSendToPipeline([book.id])}} style={{fontSize:10,fontWeight:600,color:t.purt,background:t.pur+"22",border:`1px solid ${t.pur}44`,borderRadius:4,padding:"2px 6px",cursor:"pointer"}}>⬇</button>:null}</div></td>:null}</tr>}

export function BGrid({books,onAction,onBookClick,showAuthor,highlightAuthorId,showMamLink,onSendToPipeline,selMode,sel,onToggleSel,onAuthorClick}:BookViewListProps){return<div className="book-grid" style={{display:"flex",flexWrap:"wrap",gap:12,alignItems:"start"}}>{books.map((b:Book)=><BCard key={b.id} book={b} onAction={onAction} onClick={onBookClick} showAuthor={showAuthor} highlightAuthorId={highlightAuthorId} showMamLink={showMamLink} onSendToPipeline={onSendToPipeline} selMode={selMode} selected={sel&&sel.has(b.id)} onToggleSel={onToggleSel} onAuthorClick={onAuthorClick}/>)}</div>}

export function BList({books,onAction,onBookClick,showAuthor=false,highlightAuthorId,showMamLink,onSendToPipeline,selMode,sel,onToggleSel,onAuthorClick}:BookViewListProps){const t=useTheme();return<div style={{overflowX:"auto",WebkitOverflowScrolling:"touch"}}><table style={{width:"100%",minWidth:680,borderCollapse:"collapse"}}><thead><tr style={{borderBottom:`2px solid ${t.border}`}}><th style={{padding:"8px 12px",textAlign:"left",fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Title</th>{showAuthor?<th style={{padding:"8px 12px",textAlign:"left",fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Author</th>:null}<th style={{padding:"8px 12px",textAlign:"left",fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Series</th><th style={{padding:"8px 12px",textAlign:"left",fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Date</th><th style={{padding:"8px 12px",textAlign:"left",fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>Source</th>{showMamLink?<th style={{padding:"8px 12px",textAlign:"left",fontSize:11,fontWeight:600,color:t.tg,textTransform:"uppercase"}}>MAM</th>:null}</tr></thead><tbody>{books.map((b:Book)=><BListRow key={b.id} book={b} onAction={onAction} onClick={onBookClick} showAuthor={showAuthor} highlightAuthorId={highlightAuthorId} showMamLink={showMamLink} onSendToPipeline={onSendToPipeline} selMode={selMode} selected={sel&&sel.has(b.id)} onToggleSel={onToggleSel} onAuthorClick={onAuthorClick}/>)}</tbody></table></div>}
