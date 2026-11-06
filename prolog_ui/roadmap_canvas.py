"""Offline, unified study roadmap canvas; local view changes never save records."""

from copy import deepcopy
from datetime import date
import re

import streamlit as st


_HTML = """
<section class="rc-root" aria-label="学習工程と日程">
  <div class="rc-toolbar"></div>
  <div class="rc-search-results" hidden></div>
  <div class="rc-layout">
    <div class="rc-main"><div class="rc-scroller"><div class="rc-axis"></div><div class="rc-milestones"></div><div class="rc-rows"></div></div><div class="rc-capacity"></div></div>
    <aside class="rc-inspector" aria-label="選択した工程の詳細"></aside>
  </div>
  <div class="rc-legend"><span>バー＝予定期間　数量進捗＝左列　青線＝今日</span><span class="rc-status" role="status" aria-live="polite"></span></div>
</section>
"""

_CSS = r"""
.rc-root{--rc-left:300px;font:14px var(--st-font,sans-serif);color:var(--rc-ink);background:var(--rc-card);max-width:100%;box-sizing:border-box;border:1px solid var(--rc-line);border-radius:10px;overflow:hidden}
.rc-root *{box-sizing:border-box}
.rc-root button,.rc-root input,.rc-root select{font:inherit;color:var(--rc-ink)}
.rc-root button{border:1px solid var(--rc-line);background:var(--rc-card);border-radius:6px;padding:7px 10px;min-height:36px;cursor:pointer;line-height:1.4}
.rc-root button:hover{background:var(--rc-soft)}
.rc-root button[aria-pressed=true]{background:var(--rc-selected)}
.rc-root button:disabled{cursor:default;opacity:.6}
.rc-root button:focus-visible,.rc-root input:focus-visible,.rc-root select:focus-visible{outline:2px solid var(--rc-primary);outline-offset:2px}
.rc-toolbar{display:flex;gap:8px 14px;flex-wrap:wrap;padding:12px;align-items:center;border-bottom:1px solid var(--rc-line)}
.rc-toolbar label{font-size:12px;display:flex;align-items:center;gap:5px;color:var(--rc-muted)}
.rc-toolbar input[type=search],.rc-toolbar select{min-height:36px;padding:6px 8px;border:1px solid var(--rc-line);background:var(--rc-card);border-radius:6px;max-width:180px}
.rc-toolbar input[type=search]{width:180px}
.rc-toolbar input[type=checkbox]{accent-color:var(--rc-primary)}
.rc-toolbar .rc-count{margin-left:auto;font-size:12px;color:var(--rc-muted)}
.rc-view-help{font-size:12px;color:var(--rc-muted)}
.rc-search-results{padding:10px 12px;border-bottom:1px solid var(--rc-line);font-size:12px;line-height:1.7;background:var(--rc-soft)}
.rc-search-results p{margin:0 0 5px}
.rc-search-list{display:flex;flex-direction:column;gap:5px;max-height:190px;overflow-y:auto}
.rc-search-list button{font-size:12px;text-align:left;white-space:normal;overflow-wrap:anywhere}
.rc-mode{display:flex;gap:3px}
.rc-mode button{font-size:12px;padding:6px 9px}
.rc-layout{display:grid;grid-template-columns:minmax(0,1fr) 260px;align-items:start}
.rc-main{min-width:0}
.rc-scroller{position:relative;max-height:650px;min-height:180px;overflow-y:auto;overflow-x:hidden;scrollbar-gutter:stable;overscroll-behavior:contain;background:var(--rc-card)}
.rc-axis,.rc-milestones,.rc-group,.rc-row,.rc-overview-row,.rc-capacity-grid{display:grid;grid-template-columns:var(--rc-left) minmax(0,1fr)}
.rc-axis{position:sticky;top:0;z-index:8;min-height:60px;border-bottom:1px solid var(--rc-line);background:var(--rc-card)}
.rc-axis-label{padding:12px;display:flex;justify-content:space-between;gap:10px;align-items:center;color:var(--rc-muted);font-size:12px;border-right:1px solid var(--rc-line)}
.rc-date-head{position:relative;min-height:60px;overflow:hidden;background:var(--rc-soft)}
.rc-range-label{font-size:12px;font-weight:500;padding:8px 10px 5px}
.rc-tick-row{position:relative;height:29px;border-top:1px solid var(--rc-line)}
.rc-tick{position:absolute;top:6px;font-size:12px;white-space:nowrap;color:var(--rc-muted)}
.rc-tick:not(:first-child){transform:translateX(-50%)}
.rc-tick:last-child{transform:translateX(-100%)}
.rc-milestones{min-height:38px;background:var(--rc-card);border-bottom:1px solid var(--rc-line)}
.rc-milestone-label{padding:11px 12px;color:var(--rc-muted);font-size:12px;border-right:1px solid var(--rc-line)}
.rc-milestone-lane{position:relative;min-height:38px;overflow:hidden}
.rc-root .rc-milestone-button{position:absolute;padding:3px 6px;min-height:28px;border:0;border-radius:3px;max-width:100%;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;background:var(--rc-card)}
.rc-milestone-button::before{content:'◆';font-size:11px;color:var(--rc-purple);margin-right:5px}
.rc-rows{position:relative;min-height:60px}
.rc-group{min-height:36px;background:var(--rc-soft);border-bottom:1px solid var(--rc-line)}
.rc-root .rc-group-button{display:flex;gap:7px;align-items:center;text-align:left;border:0;border-radius:0;background:transparent;min-height:36px;padding:7px 10px;font-size:12px}
.rc-group-button strong{font-weight:500;overflow-wrap:anywhere}
.rc-group-button small{font-size:11px;color:var(--rc-muted);margin-left:auto;white-space:nowrap}
.rc-group-strip{position:relative;border-left:1px solid var(--rc-line)}
.rc-group-strip::after{content:'';position:absolute;left:10px;right:10px;top:17px;height:3px;background:var(--rc-course);opacity:.55}
.rc-row{min-height:52px;border-bottom:1px solid var(--rc-line);background:var(--rc-card)}
.rc-row.rc-selected{background:var(--rc-selected)}
.rc-root .rc-item{border:0;border-radius:0;background:transparent;display:grid;grid-template-columns:minmax(0,1fr) 78px;gap:8px;align-items:center;padding:8px 10px;min-height:52px;text-align:left;overflow:hidden}
.rc-row:not(.rc-selected) .rc-item:hover{background:var(--rc-soft)}
.rc-item-title{font-size:13px;font-weight:500;line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.rc-item-meta{display:block;font-size:12px;color:var(--rc-muted);line-height:1.5;margin-top:3px;overflow-wrap:anywhere}
.rc-item-progress{text-align:right;font-size:12px;line-height:1.5;white-space:nowrap}
.rc-item-progress small{display:block;font-size:11px;color:var(--rc-muted)}
.rc-risk{color:var(--rc-red)}
.rc-lane{position:relative;min-width:0;border-left:1px solid var(--rc-line);overflow:hidden;background-image:var(--rc-day-bg);background-size:100% 100%;background-repeat:no-repeat}
.rc-today-line{position:absolute;top:0;bottom:0;border-left:2px solid var(--rc-primary);z-index:1;pointer-events:none}
.rc-root .rc-bar{position:absolute;top:50%;height:25px;min-height:25px;transform:translateY(-50%);padding:0;border:0;border-radius:5px;background:var(--rc-course);color:var(--rc-bar-text);text-align:center;z-index:3;white-space:nowrap;overflow:hidden;touch-action:pan-y}
.rc-root .rc-bar:hover{background:var(--rc-course);filter:brightness(.96)}
.rc-bar.rc-bar-selected{outline:2px solid var(--rc-ink);outline-offset:2px}
.rc-bar.rc-editable{touch-action:none;cursor:grab}
.rc-bar.rc-dragging{cursor:grabbing}
.rc-bar.rc-locked{cursor:pointer}
.rc-bar-text{display:block;padding:0 9px;font-size:12px;line-height:25px;overflow:hidden;text-overflow:ellipsis;pointer-events:none}
.rc-handle{position:absolute;top:0;bottom:0;width:10px;z-index:4;background:var(--rc-ink);opacity:.45;touch-action:none;cursor:col-resize}
.rc-handle[data-mode=start]{left:0}.rc-handle[data-mode=end]{right:0}
.rc-deadline{position:absolute;top:50%;height:10px;width:10px;background:var(--rc-card);border:2px solid var(--rc-red);transform:translate(-50%,-50%) rotate(45deg);z-index:4;pointer-events:none}
.rc-baseline{position:absolute;top:calc(50% + 18px);height:0;border-top:2px dashed var(--rc-muted);opacity:.8;pointer-events:none}
.rc-before-change{border-top-style:dotted;border-top-color:var(--rc-primary)}
.rc-change-label{position:absolute;right:5px;top:2px;font-size:11px;background:var(--rc-card);color:var(--rc-primary);z-index:5}
.rc-edges{position:absolute;left:0;top:0;overflow:hidden;pointer-events:none;z-index:2}
.rc-inspector{border-left:1px solid var(--rc-line);padding:16px;background:var(--rc-soft);min-width:0;min-height:200px}
.rc-inspector h3{font-size:16px;font-weight:500;margin:7px 0 8px;line-height:1.55;overflow-wrap:anywhere}
.rc-detail-caption{font-size:11px;letter-spacing:.6px;color:var(--rc-primary)}
.rc-detail-path{font-size:12px;color:var(--rc-muted);line-height:1.6;margin:0 0 12px;overflow-wrap:anywhere}
.rc-detail-state{display:flex;gap:8px;flex-wrap:wrap;font-size:12px;line-height:1.7;margin-bottom:14px}
.rc-detail-metrics{display:grid;grid-template-columns:1fr 1fr;gap:12px 10px;margin:12px 0}
.rc-inspector dt{font-size:12px;color:var(--rc-muted);margin-bottom:4px}
.rc-inspector dd{font-size:13px;margin:0;font-weight:500;overflow-wrap:anywhere}
.rc-detail-dates{border-top:1px solid var(--rc-line);border-bottom:1px solid var(--rc-line);padding:12px 0;font-size:12px;line-height:1.9}
.rc-detail-note{font-size:12px;line-height:1.75;white-space:pre-wrap;overflow-wrap:anywhere;margin:12px 0;color:var(--rc-muted);max-height:240px;overflow-y:auto}
.rc-detail-actions{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}
.rc-detail-actions button{font-size:12px}
.rc-related{border-top:1px solid var(--rc-line);padding-top:12px;margin-top:12px}
.rc-related h4{font-size:12px;margin:0 0 7px;font-weight:500}
.rc-related-list{display:flex;flex-direction:column;gap:5px}
.rc-related-list button{font-size:12px;text-align:left;padding:6px;overflow-wrap:anywhere}
.rc-root .rc-more{margin:8px 10px;font-size:12px}
.rc-empty{padding:24px 14px;color:var(--rc-muted);font-size:13px;line-height:1.8}
.rc-legend{display:flex;gap:10px;justify-content:space-between;flex-wrap:wrap;border-top:1px solid var(--rc-line);padding:10px 12px;font-size:11px;color:var(--rc-muted);line-height:1.6}
.rc-status{color:var(--rc-primary)}
.rc-capacity-grid{border-top:1px solid var(--rc-line);background:var(--rc-soft)}
.rc-capacity-label{padding:12px;color:var(--rc-muted);font-size:12px;line-height:1.6;border-right:1px solid var(--rc-line)}
.rc-capacity-cells{display:grid;grid-template-columns:repeat(var(--rc-buckets,4),minmax(0,1fr));min-width:0}
.rc-capacity-cell{padding:9px 6px;border-left:1px solid var(--rc-line);font-size:11px;color:var(--rc-muted);min-width:0;overflow-wrap:anywhere}
.rc-capacity-cell strong{display:block;margin-top:5px;font-size:12px;color:var(--rc-ink);font-weight:500}
.rc-capacity-cell.rc-over strong{color:var(--rc-red)}
.rc-overview-row{border-bottom:1px solid var(--rc-line);min-height:105px}
.rc-overview-name{padding:14px;border-right:1px solid var(--rc-line);font-size:13px;background:var(--rc-soft)}
.rc-overview-name strong{font-weight:500;display:block;line-height:1.6;margin-bottom:7px;overflow-wrap:anywhere}
.rc-overview-name small{display:block;color:var(--rc-muted);font-size:12px;line-height:1.6}
.rc-root.rc-narrow .rc-layout{grid-template-columns:minmax(0,1fr)}
.rc-root.rc-narrow .rc-inspector{border-left:0;border-top:1px solid var(--rc-line);min-height:0}
.rc-root.rc-narrow .rc-detail-metrics{grid-template-columns:repeat(4,minmax(0,1fr))}
.rc-root.rc-mobile{--rc-left:100%}
.rc-root.rc-mobile .rc-toolbar{padding:10px;gap:8px}
.rc-root.rc-mobile .rc-toolbar label{font-size:12px;flex-wrap:wrap}
.rc-root.rc-mobile .rc-toolbar input[type=search]{font-size:16px;width:100%;max-width:100%}
.rc-root.rc-mobile .rc-search-label{flex-basis:100%}
.rc-root.rc-mobile .rc-toolbar select{font-size:16px;max-width:220px;min-height:44px}
.rc-root.rc-mobile button{min-height:44px}
.rc-root.rc-mobile .rc-mode{flex-wrap:wrap}
.rc-root.rc-mobile .rc-count{margin-left:0}
.rc-root.rc-mobile .rc-axis{min-height:38px}
.rc-root.rc-mobile .rc-date-head{display:none}
.rc-root.rc-mobile .rc-row,.rc-root.rc-mobile .rc-group,.rc-root.rc-mobile .rc-milestones,.rc-root.rc-mobile .rc-overview-row{grid-template-columns:1fr}
.rc-root.rc-mobile .rc-row .rc-lane,.rc-root.rc-mobile .rc-group-strip,.rc-root.rc-mobile .rc-milestone-label{display:none}
.rc-root.rc-mobile .rc-item{min-height:74px;padding:11px}
.rc-root.rc-mobile .rc-item-title{font-size:14px}
.rc-root.rc-mobile .rc-item-progress,.rc-root.rc-mobile .rc-item-meta{font-size:12px}
.rc-root.rc-mobile .rc-scroller{max-height:620px;scrollbar-gutter:auto}
.rc-root.rc-mobile .rc-milestone-lane{display:flex;flex-wrap:wrap;gap:4px;padding:5px}
.rc-root.rc-mobile .rc-milestone-button{position:static;white-space:normal;max-width:100%;min-height:38px;font-size:12px}
.rc-root.rc-mobile .rc-inspector{padding:13px}
.rc-root.rc-mobile .rc-detail-metrics{grid-template-columns:1fr 1fr}
.rc-root.rc-mobile .rc-detail-actions button{min-height:44px}
.rc-root.rc-mobile .rc-capacity-grid{grid-template-columns:1fr}
.rc-root.rc-mobile .rc-capacity-cells{grid-template-columns:repeat(2,minmax(0,1fr))}
.rc-root.rc-mobile .rc-capacity-label{padding-bottom:5px;border-right:0}
.rc-root.rc-mobile .rc-edges{display:none}
@media(prefers-reduced-motion:no-preference){.rc-row{transition:background-color .1s}.rc-bar:not(.rc-dragging){transition:filter .1s}}
"""

_JS = r"""
const DAY=86400000, NS='http://www.w3.org/2000/svg';
const instances=new Map();
const dayNumber=value=>{const a=String(value).split('-').map(Number);return Date.UTC(a[0],a[1]-1,a[2])/DAY;};
const iso=value=>new Date(value*DAY).toISOString().slice(0,10);
const shortDate=value=>String(value).slice(5).replace('-','/');
const MIN=dayNumber('1900-01-01'),MAX=dayNumber('2199-12-31');
const lifecycle={not_started:'未着手',in_progress:'進行中',done:'完了',pending:'保留'};
const hash=value=>{let h=2166136261;for(const c of String(value)){h^=c.codePointAt(0);h=Math.imul(h,16777619);}return h>>>0;};

export default function(component){
 const {parentElement,key,data}=component,doc=parentElement.ownerDocument,win=doc.defaultView;
 const root=parentElement.querySelector('.rc-root'),toolbar=root.querySelector('.rc-toolbar'),searchNode=root.querySelector('.rc-search-results'),scroller=root.querySelector('.rc-scroller'),rowsNode=root.querySelector('.rc-rows'),axisNode=root.querySelector('.rc-axis'),milestonesNode=root.querySelector('.rc-milestones'),inspectorNode=root.querySelector('.rc-inspector'),capacityNode=root.querySelector('.rc-capacity'),statusNode=root.querySelector('.rc-status');
 const defaults={ink:'#152238',card:'#ffffff',line:'#d9e2ec',primary:'#003b71',muted:'#667085',cyan:'#1f6f9f',green:'#237b64',amber:'#a66d16',red:'#b54747',purple:'#7357a8'};
 const palette={...defaults};for(const name of Object.keys(defaults))if(/^#[0-9a-f]{6}$/i.test(data.palette?.[name]||''))palette[name]=data.palette[name];
 for(const [name,value]of Object.entries(palette))root.style.setProperty('--rc-'+name,value);
 root.style.setProperty('--rc-soft',data.palette?.mode==='dark'?'#1d2c3d':'#f2f5f9');root.style.setProperty('--rc-selected',data.palette?.mode==='dark'?'#21405d':'#e8f2ff');
 let state=instances.get(key);
 if(!state){state={selected:'',query:'',project:'',subject:'',risk:false,archived:false,folded:new Set(),expandedLimit:120,viewMode:data.view_mode||'detail',editable:Boolean(data.editable),tool:'move',scrollTop:0,scrollLeft:0,clientPending:false,lastRequest:null,sourceRevision:data.source_revision,externalSelected:'',externalMode:data.view_mode||'detail',externalEditable:Boolean(data.editable),message:'',observer:null,drag:null};
  try{const saved=JSON.parse(win.sessionStorage.getItem('proloag-roadmap-canvas:'+key)||'null');if(saved&&saved.version===1){for(const field of ['selected','query','project','subject','risk','archived','viewMode','editable','scrollTop'])if(typeof saved[field]===typeof state[field])state[field]=saved[field];if(Array.isArray(saved.folded))state.folded=new Set(saved.folded.filter(x=>typeof x==='string'));}}catch{}
  instances.set(key,state);
 }
 if(state.observer)state.observer.disconnect();if(state.scrollTimer)win.clearTimeout(state.scrollTimer);
 state.component=component;state.data=data;state.disposed=false;
 if(state.sourceRevision!==data.source_revision){state.sourceRevision=data.source_revision;state.clientPending=false;state.lastRequest=null;state.drag=null;state.message='';}
 if(state.lastRequest&&state.lastRequest.event_id===data.ack_event_id){state.clientPending=false;state.message=data.pending?'変更案を確認し、保存または取り消しを選んでください。':'';}
 if(data.selected_id&&data.selected_id!==state.externalSelected){state.selected=data.selected_id;state.externalSelected=data.selected_id;state.focusRequested=true;}
 if(state.externalMode!==data.view_mode){state.viewMode=data.view_mode||'detail';state.externalMode=data.view_mode;}
 if(state.externalEditable!==Boolean(data.editable)){state.editable=Boolean(data.editable);state.externalEditable=Boolean(data.editable);}
 const allItems=Array.isArray(data.items)?data.items:[],allMilestones=Array.isArray(data.milestones)?data.milestones:[],itemMap=new Map(allItems.map(item=>[item.id,item])),milestoneMap=new Map(allMilestones.map(item=>[item.id,item]));
 const start=dayNumber(data.start_date),end=dayNumber(data.end_date)+1,span=Math.max(1,end-start),today=dayNumber(data.today);
 const node=(tag,cls,text)=>{const value=doc.createElement(tag);if(cls)value.className=cls;if(text!==undefined)value.textContent=String(text);return value;};
 const svg=(tag,attributes)=>{const value=doc.createElementNS(NS,tag);for(const [name,val]of Object.entries(attributes||{}))value.setAttribute(name,String(val));return value;};
 function button(text,cls,callback){const value=node('button',cls,text);value.type='button';if(callback)value.onclick=callback;return value;}
 function say(message){state.message=message;statusNode.textContent=message;}
 function remember(){try{win.sessionStorage.setItem('proloag-roadmap-canvas:'+key,JSON.stringify({version:1,selected:state.selected,query:state.query,project:state.project,subject:state.subject,risk:state.risk,archived:state.archived,folded:[...state.folded],viewMode:state.viewMode,editable:state.editable,scrollTop:scroller.scrollTop}));}catch{}}
 function disabled(){return Boolean(data.pending||state.clientPending);}
 function eventId(){return win.crypto.randomUUID().replaceAll('-','');}
 function emit(kind,values={}){if(state.clientPending||(data.pending&&kind!=='focus_item'))return;const request={kind,event_id:eventId(),source_revision:data.source_revision,...values};state.lastRequest=request;state.clientPending=true;say(kind==='schedule_change'?'変更案を確認しています。':'選択した操作を開いています。');updateControls();component.setTriggerValue('action',request);}
 function name(item,level){const keys=['project','subject','material'];return item['view_'+level]||data.names?.[item[level+'_id']]||(item[level+'_id']?'名称未確認':'未設定');}
 function path(item){return ['project','subject','material'].map(level=>name(item,level)).filter((value,index,values)=>value!=='未設定'||values.length===1).join(' / ');}
 function stateLabel(item){return item.view_state_label||lifecycle[item.status]||'状態未確認';}
 function riskLabel(item){if(typeof item.view_risk_label==='string')return item.view_risk_label;if(item.status==='done')return '';if(item.deadline&&dayNumber(item.deadline)<today)return '確定期限超過';return dayNumber(item.end_date)<today?'終了予定日超過':'';}
 function needsAttention(item){return typeof item.view_needs_attention==='boolean'?item.view_needs_attention:Boolean(riskLabel(item));}
 function progressLabel(item){if(item.view_progress_label)return item.view_progress_label;return `${item.completed} / ${item.total} ${item.unit||''}`;}
 function actualLabel(item){if(typeof item.view_actual_label==='string')return item.view_actual_label;const counts=data.actuals?.counts?.[item.id]||0;if(counts>0)return `${Math.round((data.actuals?.seconds?.[item.id]||0)/60)}分`;return (data.actuals?.task_counts?.[item.id]||0)>0?'記録なし':'未連携';}
 function color(item){if(item.status==='done')return palette.muted;const identity=item.view_group_color_key||item.subject_id||item.project_id;if(!identity||identity==='unassigned')return palette.primary;const colors=[palette.cyan,palette.purple,palette.green,palette.amber,palette.primary];return colors[hash(identity)%colors.length];}
 function textOn(fill){const a=[1,3,5].map(i=>parseInt(fill.slice(i,i+2),16)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4);return (a[0]*.2126+a[1]*.7152+a[2]*.0722)>.36?'#06111f':'#ffffff';}
 function selectedItem(){return itemMap.get(state.selected);}
 function pathKeys(item){const values=[];return ['project','subject','material'].map(level=>{values.push(item[level+'_id']||'');return JSON.stringify(values);});}
 function expandPath(item){for(const value of pathKeys(item))state.folded.delete(value);}
 function select(item,focus=false){const fromBar=doc.activeElement?.classList?.contains('rc-bar'),fromItem=doc.activeElement?.classList?.contains('rc-item');state.selected=item.id;if(focus||!state.query.trim())expandPath(item);state.message=`${item.title}を選択`;if(focus){state.project='';state.subject='';state.query='';state.risk=false;state.archived=Boolean(item.is_archived);state.expandedLimit=Math.max(state.expandedLimit,allItems.length);state.focusRequested=true;if(dayNumber(item.end_date)<start||dayNumber(item.start_date)>=end){remember();emit('focus_item',{item_id:item.id});return;}}remember();render();const record=state.records.get(item.id);if(record&&(focus||fromBar||fromItem)){const target=fromBar?record.bar:record.lane.parentElement.querySelector('.rc-item');target?.focus({preventScroll:true});}}
 function allFiltered(){const query=state.query.trim().toLocaleLowerCase();return allItems.filter(item=>(state.archived||!item.is_archived)&&(!state.project||item.project_id===state.project)&&(!state.subject||item.subject_id===state.subject)&&(!state.risk||needsAttention(item))&&(!query||`${item.title} ${path(item)} ${item.view_unit||''}`.toLocaleLowerCase().includes(query)));}
 function visibleItems(){return allFiltered().filter(item=>dayNumber(item.start_date)<end&&dayNumber(item.end_date)>=start).sort((a,b)=>String(a.project_id).localeCompare(String(b.project_id))||String(a.subject_id).localeCompare(String(b.subject_id))||String(a.material_id).localeCompare(String(b.material_id))||a.start_date.localeCompare(b.start_date)||a.title.localeCompare(b.title)||a.id.localeCompare(b.id));}
 function searchResults(values){searchNode.replaceChildren();searchNode.hidden=!state.query.trim();if(searchNode.hidden)return;const outside=allFiltered().filter(item=>dayNumber(item.start_date)>=end||dayNumber(item.end_date)<start).sort((a,b)=>a.start_date.localeCompare(b.start_date)||a.title.localeCompare(b.title));searchNode.append(node('p','',`検索結果：表示期間内 ${values.length}工程 ／ 期間外 ${outside.length}工程。分類などの絞り込み条件も適用しています。`));if(values.length&&state.viewMode==='detail')searchNode.append(node('p','','検索中は、該当する分類を開いて表示します。検索を消すと元に戻ります。'));if(outside.length){searchNode.append(node('p','','期間外の工程名を選ぶと、その期間へ移動します。'));const list=node('div','rc-search-list');for(const item of outside){const b=button(`${item.title}　${item.start_date}〜${item.end_date}を表示`,'',()=>{state.viewMode='detail';select(item,true);});b.disabled=state.clientPending;list.append(b);}searchNode.append(list);}}
 function selectControl(label,values,current,onChange){const wrap=node('label','',label),select=node('select');select.setAttribute('aria-label',label);select.dataset.control=label;for(const [value,text]of values){const option=node('option','',text);option.value=value;select.append(option);}select.value=current;select.onchange=()=>onChange(select.value);wrap.append(select);return wrap;}
 function checkbox(label,checked,onChange){const wrap=node('label'),input=node('input');input.type='checkbox';input.dataset.control=label;input.checked=checked;input.onchange=()=>onChange(input.checked);wrap.append(input,doc.createTextNode(label));return wrap;}
 function renderToolbar(){toolbar.replaceChildren();const modes=node('div','rc-mode');for(const [value,label]of [['detail','工程'],['overview','俯瞰']]){const b=button(label,'',()=>{state.viewMode=value;state.scrollTop=0;remember();render();});b.setAttribute('aria-pressed',String(state.viewMode===value));modes.append(b);}toolbar.append(modes);
  const searchWrap=node('label','rc-search-label','工程を探す'),search=node('input');search.type='search';search.placeholder='教材・工程名';search.value=state.query;search.setAttribute('aria-label','教材・工程名で検索');search.oninput=()=>{state.query=search.value;state.expandedLimit=120;remember();renderBody();};searchWrap.append(search);toolbar.append(searchWrap);
  const projects=[...new Map(allItems.filter(i=>i.project_id).map(i=>[i.project_id,name(i,'project')])).entries()];if(projects.length>1)toolbar.append(selectControl('プロジェクト',[['','すべて'],...projects],state.project,value=>{state.project=value;state.subject='';state.scrollTop=0;remember();render();}));
  const subjects=[...new Map(allItems.filter(i=>i.subject_id&&(!state.project||i.project_id===state.project)).map(i=>[i.subject_id,name(i,'subject')])).entries()];if(subjects.length>1)toolbar.append(selectControl('科目',[['','すべて'],...subjects],state.subject,value=>{state.subject=value;state.scrollTop=0;remember();render();}));
  toolbar.append(checkbox('要確認だけ',state.risk,value=>{state.risk=value;state.scrollTop=0;remember();render();}),checkbox('アーカイブ',state.archived,value=>{state.archived=value;remember();render();}));
  if(state.viewMode==='detail'){const edit=checkbox('日程を編集',state.editable&&!state.mobile,value=>{state.editable=value;remember();render();});edit.querySelector('input').disabled=disabled()||state.mobile;toolbar.append(edit);if(state.editable&&!state.mobile)toolbar.append(selectControl('矢印キーの操作',[['move','工程を移動'],['start','開始日を調整'],['end','終了日を調整']],state.tool,value=>{state.tool=value;remember();}));}else toolbar.append(node('span','rc-view-help','バーを動かすには「工程」を選んでください。'));
  toolbar.append(node('span','rc-count'));}
 function updateControls(){toolbar.querySelectorAll('input[type=checkbox]').forEach(input=>{if(input.parentElement.textContent.includes('日程を編集'))input.disabled=disabled()||state.mobile;});root.querySelectorAll('[data-server-action]').forEach(b=>b.disabled=disabled()||b.dataset.archivedLocked==='true');searchNode.querySelectorAll('button').forEach(b=>b.disabled=state.clientPending);}
 function axis(){axisNode.replaceChildren();const left=node('div','rc-axis-label');left.append(node('span','',state.viewMode==='overview'?'科目・教材':'工程・状態'),node('span','',state.viewMode==='overview'?'工程数':'数量進捗'));const right=node('div','rc-date-head');right.append(node('div','rc-range-label',`${data.start_date.replaceAll('-','/')} — ${data.end_date.replaceAll('-','/')}`));const ticks=node('div','rc-tick-row');const width=Math.max(180,state.width-300-(state.narrow?0:260));const count=Math.max(2,Math.min(10,Math.floor(width/83)));for(let i=0;i<count;i++){const day=Math.round(start+(span-1)*i/(count-1)),tick=node('span','rc-tick',shortDate(iso(day)));tick.style.left=(day-start)/span*100+'%';ticks.append(tick);}right.append(ticks);axisNode.append(left,right);}
 function dayBackground(){const values=[];const lineColor=palette.line,soft=data.palette?.mode==='dark'?'#203044':'#f1f4f8';const weekday=new Date(start*DAY).getUTCDay();if(span<=120){for(let offset=0;offset<span;offset++){const d=start+offset,date=iso(d),w=(weekday+offset)%7;const configured=data.calendar?.overrides?.[date]??data.calendar?.weekly_minutes?.[(w+6)%7];if(w===0||w===6||configured===0){const a=offset/span*100,b=(offset+1)/span*100;values.push(`linear-gradient(to right,transparent ${a}%,${soft} ${a}%,${soft} ${b}%,transparent ${b}%)`);}}}const step=span<=14?1:span<=120?7:Math.max(7,Math.ceil(span/16));values.push(`repeating-linear-gradient(to right,${lineColor} 0,${lineColor} 1px,transparent 1px,transparent ${step/span*100}%)`);root.style.setProperty('--rc-day-bg',values.join(','));}
 function decorateLane(lane){if(today>=start&&today<end){const line=node('span','rc-today-line');line.style.left=(today-start+.5)/span*100+'%';lane.append(line);}}
 function milestones(){milestonesNode.replaceChildren();const values=allMilestones.filter(item=>(state.archived||!item.is_archived)&&dayNumber(item.date)>=start&&dayNumber(item.date)<end&&(!state.project||item.project_id===state.project));if(!values.length){milestonesNode.hidden=true;return;}milestonesNode.hidden=false;milestonesNode.append(node('div','rc-milestone-label','試験・節目'));const lane=node('div','rc-milestone-lane'),ends=[];const usable=Math.max(130,state.width-(state.narrow?0:260)-300);for(const item of values.sort((a,b)=>a.date.localeCompare(b.date)||a.title.localeCompare(b.title))){const day=dayNumber(item.date),label=shortDate(item.date)+' '+item.title;const b=button(label,'rc-milestone-button',()=>{if(!disabled())emit('edit_milestone',{milestone_id:item.id});});b.dataset.milestoneId=item.id;b.setAttribute('aria-label',label+'を編集');b.dataset.serverAction='true';b.disabled=disabled();if(!state.mobile){const width=Math.min(usable,Math.max(100,Math.min(260,22+label.length*7))),x=Math.max(0,Math.min(usable-width,(day-start)/span*usable));let row=ends.findIndex(value=>value+8<=x);if(row<0)row=ends.length;ends[row]=x+width;b.style.left=x/usable*100+'%';b.style.top=5+row*32+'px';b.style.width=width+'px';}lane.append(b);}if(!state.mobile)lane.style.height=Math.max(38,ends.length*32+8)+'px';milestonesNode.append(lane);}
 function dateChange(item,delta,mode){let a=dayNumber(item.start_date),b=dayNumber(item.end_date);if(mode==='move'){delta=Math.max(MIN-a,Math.min(MAX-b,delta));a+=delta;b+=delta;}else if(mode==='start')a=Math.max(MIN,Math.min(b,a+delta));else b=Math.min(MAX,Math.max(a,b+delta));return {start_date:iso(a),end_date:iso(b)};}
 function position(record,value){const a=dayNumber(value.start_date),b=dayNumber(value.end_date)+1,left=Math.max(start,a),right=Math.min(end,b);record.current=value;record.bar.style.left=(left-start)/span*100+'%';record.bar.style.width=Math.max(0,right-left)/span*100+'%';record.bar.hidden=right<=left;record.bar.setAttribute('aria-label',`${record.item.title}、${value.start_date}から${value.end_date}、${progressLabel(record.item)}`);}
 function edges(){if(state.edgeNode)state.edgeNode.remove();if(state.mobile||state.viewMode!=='detail')return;const selected=selectedItem();if(!selected)return;const svgNode=svg('svg',{class:'rc-edges',width:rowsNode.clientWidth,height:rowsNode.scrollHeight,'aria-hidden':'true'}),base=rowsNode.getBoundingClientRect();const relatives=[];for(const id of selected.predecessor_ids||[])relatives.push([id,selected.id]);for(const item of allItems)if((item.predecessor_ids||[]).includes(selected.id))relatives.push([selected.id,item.id]);for(const [parentId,childId]of relatives){const parent=state.records.get(parentId),child=state.records.get(childId);if(!parent||!child)continue;const pa=parent.lane.getBoundingClientRect(),ch=child.lane.getBoundingClientRect(),x1=pa.left-base.left+Math.min(span,Math.max(0,dayNumber(parent.current.end_date)+1-start))/span*pa.width,x2=ch.left-base.left+Math.min(span,Math.max(0,dayNumber(child.current.start_date)-start))/span*ch.width,y1=pa.top-base.top+pa.height/2,y2=ch.top-base.top+ch.height/2,mid=Math.max(x1+8,(x1+x2)/2);svgNode.append(svg('path',{d:`M${x1},${y1} L${mid},${y1} L${mid},${y2} L${x2},${y2}`,stroke:palette.primary,'stroke-width':1.5,fill:'none'}));svgNode.append(svg('path',{d:`M${x2-5},${y2-4} L${x2},${y2} L${x2-5},${y2+4}`,stroke:palette.primary,'stroke-width':1.5,fill:'none'}));}rowsNode.append(svgNode);state.edgeNode=svgNode;}
 function finishDrag(cancel=false){const drag=state.drag;if(!drag)return;state.drag=null;try{if(drag.bar.hasPointerCapture(drag.pointerId))drag.bar.releasePointerCapture(drag.pointerId);}catch{}drag.bar.classList.remove('rc-dragging');if(cancel){position(drag.record,drag.item);edges();say('日程の変更を取り消しました。');return;}const change=drag.change||{start_date:drag.item.start_date,end_date:drag.item.end_date};if(change.start_date===drag.item.start_date&&change.end_date===drag.item.end_date)return;state.suppressClick=true;emit('schedule_change',{item_id:drag.item.id,old_start:drag.item.start_date,old_end:drag.item.end_date,...change});}
 function addBar(lane,item,top){const bar=button('','rc-bar',()=>{if(state.suppressClick){state.suppressClick=false;return;}select(item);});bar.dataset.itemId=item.id;bar.style.setProperty('--rc-course',color(item));bar.style.setProperty('--rc-bar-text',textOn(color(item)));if(state.selected===item.id)bar.classList.add('rc-bar-selected');const locked=item.is_archived||item.status==='done',canEdit=state.viewMode==='detail'&&state.editable&&!locked&&!state.mobile&&!disabled();bar.classList.toggle('rc-locked',locked);bar.classList.toggle('rc-editable',canEdit);if(top!==undefined){bar.style.top=top+'px';bar.style.transform='none';}
  const text=node('span','rc-bar-text',item.title);text.setAttribute('aria-hidden','true');bar.append(text);const record={item,bar,lane,current:item};if(top===undefined)state.records.set(item.id,record);position(record,item);
  if(canEdit){for(const mode of ['start','end']){if((mode==='start'&&dayNumber(item.start_date)<start)||(mode==='end'&&dayNumber(item.end_date)>=end))continue;const grip=node('span','rc-handle');grip.dataset.mode=mode;grip.setAttribute('aria-hidden','true');bar.append(grip);}}
  bar.onpointerdown=event=>{if(!canEdit||disabled()||event.button!==0||top!==undefined)return;event.preventDefault();state.selected=item.id;renderInspector();const mode=event.target.dataset.mode||'move';state.drag={item,record,bar,pointerId:event.pointerId,startX:event.clientX,mode,change:null};bar.classList.add('rc-dragging');bar.setPointerCapture(event.pointerId);bar.focus();};
  bar.onpointermove=event=>{const drag=state.drag;if(!drag||event.pointerId!==drag.pointerId)return;event.preventDefault();const delta=Math.round((event.clientX-drag.startX)/(lane.getBoundingClientRect().width/span));drag.change=dateChange(item,delta,drag.mode);position(record,drag.change);edges();say(`${item.title}：${drag.change.start_date} — ${drag.change.end_date}`);};
  bar.onpointerup=()=>finishDrag(false);bar.onpointercancel=()=>finishDrag(true);bar.onlostpointercapture=()=>{if(state.drag)finishDrag(true);};
  bar.onkeydown=event=>{if(event.key==='Escape'){finishDrag(true);return;}if(!canEdit||disabled()||event.repeat||!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();const delta=(event.key==='ArrowRight'?1:-1)*(event.shiftKey?7:1),change=dateChange(item,delta,state.tool);if(change.start_date===item.start_date&&change.end_date===item.end_date)return;state.selected=item.id;position(record,change);edges();emit('schedule_change',{item_id:item.id,old_start:item.start_date,old_end:item.end_date,...change});};
  function comparison(a,b,cls,label,offset){if(!a||!b||(a===item.start_date&&b===item.end_date))return;const left=Math.max(start,dayNumber(a)),right=Math.min(end,dayNumber(b)+1);if(right<=left)return;const old=node('span','rc-baseline '+cls);old.style.left=(left-start)/span*100+'%';old.style.width=(right-left)/span*100+'%';old.style.top=top!==undefined?(top+offset)+'px':`calc(50% + ${offset-12}px)`;old.setAttribute('aria-label',label+' '+a+'から'+b);lane.append(old);}
  if(data.show_baseline)comparison(item.baseline_start_date,item.baseline_end_date,'rc-original-plan','当初予定',32);
  if(item.view_changed||item.view_is_draft)comparison(item.view_before_start||item.view_saved_start_date,item.view_before_end||item.view_saved_end_date,'rc-before-change','保存済み予定（変更案との比較）',28);
  if(item.deadline&&dayNumber(item.deadline)>=start&&dayNumber(item.deadline)<end){const deadline=node('span','rc-deadline');deadline.style.left=(dayNumber(item.deadline)-start+.5)/span*100+'%';deadline.setAttribute('aria-label','確定期限 '+item.deadline);lane.append(deadline);}lane.append(bar);return record;
 }
 function itemRow(item){const row=node('div','rc-row'+(state.selected===item.id?' rc-selected':''));row.dataset.itemRow=item.id;const left=button('','rc-item',()=>select(item));left.setAttribute('aria-pressed',String(state.selected===item.id));left.setAttribute('aria-label',item.title+'、'+stateLabel(item)+'、'+progressLabel(item));const description=node('span'),title=node('span','rc-item-title',(needsAttention(item)?'！ ':'')+item.title),meta=node('span','rc-item-meta',stateLabel(item)+(riskLabel(item)?' · '+riskLabel(item):'')+'　'+shortDate(item.start_date)+'–'+shortDate(item.end_date));if(needsAttention(item))meta.classList.add('rc-risk');description.append(title,meta);const quantity=node('span','rc-item-progress',progressLabel(item));if(Number.isFinite(item.view_progress_percent))quantity.append(node('small','',Math.round(item.view_progress_percent)+'%'));left.append(description,quantity);const lane=node('div','rc-lane');decorateLane(lane);addBar(lane,item);if(item.view_changed||item.view_is_draft){lane.append(node('span','rc-change-label','変更案'));}row.append(left,lane);return row;}
 function groupTree(values){const roots=new Map();for(const item of values){let branch=roots;const keyParts=[];for(const [level,index]of [['project',0],['subject',1],['material',2]]){const id=item[level+'_id']||'',label=name(item,level);keyParts.push(id);const groupKey=JSON.stringify(keyParts);if(!branch.has(id))branch.set(id,{id:groupKey,label,level:index,items:[],children:new Map()});const group=branch.get(id);group.items.push(item);branch=group.children;} }return roots;}
 function groupHeader(group){const searching=Boolean(state.query.trim()),folded=!searching&&state.folded.has(group.id),header=node('div','rc-group'),b=button('','rc-group-button',()=>{if(searching)return;if(state.folded.has(group.id))state.folded.delete(group.id);else state.folded.add(group.id);remember();renderBody();for(const target of rowsNode.querySelectorAll('[data-group-key]'))if(target.dataset.groupKey===group.id){target.focus({preventScroll:true});break;}});b.disabled=searching;b.style.paddingLeft=10+group.level*11+'px';b.setAttribute('aria-expanded',String(!folded));b.dataset.groupKey=group.id;b.append(node('span','',folded?'›':'⌄'),node('strong','',group.label),node('small','',group.items.length+'工程 · 完了'+group.items.filter(item=>item.status==='done').length));const strip=node('div','rc-group-strip');strip.style.setProperty('--rc-course',color(group.items[0]));header.append(b,strip);return header;}
 function renderDetail(values){const tree=groupTree(values);let drawn=0;function walk(branch){for(const group of branch.values()){const redundant=group.level<2&&branch.size===1&&(group.level===0||group.label==='未設定');if(!redundant){rowsNode.append(groupHeader(group));if(!state.query.trim()&&state.folded.has(group.id))continue;}if(group.level<2)walk(group.children);else for(const item of group.items){if(drawn>=state.expandedLimit)continue;rowsNode.append(itemRow(item));drawn++;}}}walk(tree);if(drawn>=state.expandedLimit&&drawn<values.length){const more=button(`さらに${Math.min(120,values.length-drawn)}工程を表示（現在${drawn}/${values.length}工程）`,'rc-more',()=>{state.expandedLimit+=120;remember();renderBody();});rowsNode.append(more);}return drawn;}
 function renderOverview(values){const groups=new Map();for(const item of values){const id=JSON.stringify([item.project_id||'',item.subject_id||'']);if(!groups.has(id))groups.set(id,[]);groups.get(id).push(item);}for(const groupItems of groups.values()){const row=node('div','rc-overview-row'),left=node('div','rc-overview-name');left.append(node('strong','',name(groupItems[0],'subject')==='未設定'?name(groupItems[0],'project'):name(groupItems[0],'subject')),node('small','',groupItems.length+'工程 · 完了'+groupItems.filter(item=>item.status==='done').length+'工程'));const show=button('工程を見る','',()=>{state.viewMode='detail';state.subject=groupItems[0].subject_id||'';state.project=groupItems[0].project_id||'';state.selected=groupItems[0].id;expandPath(groupItems[0]);remember();render();});left.append(show);const lane=node('div','rc-lane');decorateLane(lane);const ends=[];for(const item of groupItems.slice(0,120)){let level=ends.findIndex(end=>end<dayNumber(item.start_date));if(level<0)level=ends.length;ends[level]=dayNumber(item.end_date);addBar(lane,item,8+level*33);}lane.style.height=Math.max(105,Math.min(ends.length,120)*33+16)+'px';if(groupItems.length>120)left.append(node('small','',`期間バーは先頭120工程。工程表示から全${groupItems.length}件を確認できます。`));row.append(left,lane);rowsNode.append(row);}return values.length;}
 function metric(label,text){const wrap=node('div');wrap.append(node('dt','',label),node('dd','',text));return wrap;}
 function serverButton(label,kind,values){const b=button(label,'',()=>emit(kind,kind==='show_capacity'?{}:values));b.dataset.serverAction='true';b.dataset.archivedLocked=String(kind==='allocate_item'&&Boolean(itemMap.get(values?.item_id)?.is_archived));b.disabled=disabled()||b.dataset.archivedLocked==='true';return b;}
 function renderInspector(){inspectorNode.replaceChildren();const item=selectedItem();if(!item){inspectorNode.append(node('p','rc-empty','工程を選ぶと、進捗・期限・関連タスクを確認できます。'));return;}inspectorNode.append(node('div','rc-detail-caption','選択した工程'),node('h3','',item.title),node('p','rc-detail-path',path(item)));if(state.visibleIds&&!state.visibleIds.has(item.id)){const outside=node('p','rc-detail-note','選択中の工程は現在の期間・絞り込みの外にあります。');outside.append(button('表示して確認','',()=>select(item,true)));inspectorNode.append(outside);}const lifecycleNode=node('div','rc-detail-state');lifecycleNode.append(node('span','',stateLabel(item)));if(riskLabel(item))lifecycleNode.append(node('span','rc-risk','！'+riskLabel(item)));if(item.is_archived)lifecycleNode.append(node('span','','アーカイブ'));inspectorNode.append(lifecycleNode);const metrics=node('dl','rc-detail-metrics');metrics.append(metric('数量進捗',progressLabel(item)),metric('学習予定',Math.round(item.planned_minutes/60*100)/100+'時間'),metric('関連ログ累計',actualLabel(item)),metric('日別タスク',(data.actuals?.task_counts?.[item.id]??item.view_task_count??0)+'件'));inspectorNode.append(metrics);const dates=node('div','rc-detail-dates');for(const [label,value]of [['開始',item.start_date],['終了予定',item.end_date],['確定期限',item.deadline||'未設定']])dates.append(node('div','',label+'　'+value));inspectorNode.append(dates);if(item.memo)inspectorNode.append(node('p','rc-detail-note',item.memo));const actions=node('div','rc-detail-actions');actions.append(serverButton('工程を編集','edit_item',{item_id:item.id}),serverButton('日別タスクへ割当','allocate_item',{item_id:item.id}),serverButton('負荷・配分を開く','show_capacity',{item_id:item.id}));inspectorNode.append(actions);
  const parents=item.predecessor_ids||[],children=allItems.filter(value=>(value.predecessor_ids||[]).includes(item.id));for(const [label,ids]of [['前提工程',parents],['後続工程',children.map(value=>value.id)]]){const section=node('div','rc-related');section.append(node('h4','',label+' '+ids.length+'件'));const list=node('div','rc-related-list');for(const id of ids){const related=itemMap.get(id),title=related?.title||data.names?.[id]||'表示外の工程';const b=button(title+(state.records.has(id)?'':'（表示外へ移動）'),'',()=>{if(related)select(related,true);else emit('focus_item',{item_id:id});});b.disabled=state.clientPending;list.append(b);}section.append(list);inspectorNode.append(section);}if(state.mobile)inspectorNode.append(node('p','rc-detail-note','日程は「工程を編集」から変更できます。小さな画面ではドラッグせず、日付を入力します。'));}
 function capacity(){capacityNode.replaceChildren();capacityNode.style.paddingRight=state.mobile?'0px':Math.max(0,scroller.offsetWidth-scroller.clientWidth)+'px';let values=Array.isArray(data.workload)?data.workload:data.workload?.rows;if(!Array.isArray(values)){const b=serverButton('日別配分の負荷を確認','show_capacity',{});b.classList.add('rc-more');capacityNode.append(b);return;}values=values.filter(row=>row.date&&dayNumber(row.date)>=start&&dayNumber(row.date)<end);if(!values.length){capacityNode.append(node('div','rc-empty','この期間の日別配分・学習可能時間は未設定です。'));return;}const usable=Math.max(160,state.width-(state.narrow?0:260)-(state.mobile?0:300)),maxBuckets=Math.max(1,Math.min(14,Math.floor(usable/78))),bucketSize=span<=14&&usable/span>=48?1:Math.max(7,Math.ceil(span/maxBuckets/7)*7),buckets=new Map();for(const row of values){const index=Math.floor((dayNumber(row.date)-start)/bucketSize);if(!buckets.has(index))buckets.set(index,{date:iso(start+index*bucketSize),planned:0,capacity:0,known:true,weight:Math.min(bucketSize,span-index*bucketSize)});const bucket=buckets.get(index);bucket.planned+=Number.isFinite(row.planned_minutes)?row.planned_minutes:0;if(Number.isFinite(row.capacity_minutes))bucket.capacity+=row.capacity_minutes;else bucket.known=false;}const entries=[...buckets.entries()].sort((a,b)=>a[0]-b[0]).map(pair=>pair[1]),strip=node('div','rc-capacity-grid');strip.append(node('div','rc-capacity-label','登録済み日別予定 / 学習可能時間'));const cells=node('div','rc-capacity-cells');if(!state.mobile)cells.style.gridTemplateColumns=entries.map(bucket=>bucket.weight+'fr').join(' ');for(const bucket of entries){const over=bucket.known&&bucket.planned>bucket.capacity,cell=node('div','rc-capacity-cell'+(over?' rc-over':''),shortDate(bucket.date)+(bucketSize>1?'〜'+shortDate(iso(dayNumber(bucket.date)+bucket.weight-1)):''));cell.append(node('strong','',bucket.planned+' / '+(bucket.known?bucket.capacity:'未設定')+'分'+(over?' ！':'')));cells.append(cell);}strip.append(cells);capacityNode.append(strip);}
 function renderBody(){const scroll=state.focusRequested?0:scroller.scrollTop;rowsNode.replaceChildren();state.edgeNode=null;state.records=new Map();const values=visibleItems();searchResults(values);state.visibleIds=new Set(values.map(item=>item.id));if(!state.selected&&values.length){state.selected=values[0].id;expandPath(values[0]);}if(!state.initializedGroups){if(allItems.length>80){for(const item of allItems)for(const value of pathKeys(item))state.folded.add(value);if(selectedItem())expandPath(selectedItem());}state.initializedGroups=true;}const drawn=state.viewMode==='overview'?renderOverview(values):renderDetail(values);if(!values.length)rowsNode.append(node('div','rc-empty','この期間・条件に該当する工程はありません。検索・分類・期間を変えて確認できます。'));const count=toolbar.querySelector('.rc-count');if(count)count.textContent=`${values.length}工程 / 全${allItems.length}工程${state.viewMode==='detail'&&drawn<values.length?'（折りたたみ・未表示あり）':''}`;renderInspector();scroller.scrollTop=scroll;state.scrollTop=scroll;capacity();edges();if(state.focusRequested){state.focusRequested=false;const record=state.records.get(state.selected);if(record){const row=record.lane.parentElement;row.scrollIntoView({block:'nearest'});state.scrollTop=scroller.scrollTop;}}updateControls();statusNode.textContent=state.message|| (data.pending?'変更案を確認し、保存または取り消しを選んでください。':state.viewMode==='detail'&&state.editable&&!state.mobile?'日程編集：棒の移動・左右端の伸縮。矢印キーは1日、Shiftは7日。Escapeで取り消し。':'工程名または棒を選ぶと詳細を確認できます。');const legend=root.querySelector('.rc-legend > span:first-child');legend.textContent='バー＝予定期間　数量進捗＝左列　青線＝今日'+(data.show_baseline?'　破線＝当初予定':'')+(data.pending?'　点線＝変更前の保存済み予定':'');}
 function render(){const focused=doc.activeElement?.dataset?.control,width=Math.max(280,root.getBoundingClientRect().width);state.width=width;state.mobile=width<600;state.narrow=width<960;root.classList.toggle('rc-mobile',state.mobile);root.classList.toggle('rc-narrow',state.narrow);dayBackground();renderToolbar();axis();milestones();renderBody();if(focused)for(const target of toolbar.querySelectorAll('[data-control]'))if(target.dataset.control===focused){target.focus({preventScroll:true});break;}}
 scroller.onscroll=()=>{state.scrollTop=scroller.scrollTop;if(state.scrollTimer)win.clearTimeout(state.scrollTimer);state.scrollTimer=win.setTimeout(remember,180);};
 state.observer=new win.ResizeObserver(()=>{if(Math.abs(root.getBoundingClientRect().width-state.width)>1&&!state.drag)render();});state.observer.observe(root);
 const restoreScroll=state.focusRequested?null:state.scrollTop||0;render();if(restoreScroll!==null){scroller.scrollTop=restoreScroll;state.scrollTop=scroller.scrollTop;}
 return ()=>{if(state.drag)finishDrag(true);remember();state.disposed=true;if(state.observer)state.observer.disconnect();if(state.scrollTimer)win.clearTimeout(state.scrollTimer);};
}
"""


_KINDS = {
    "schedule_change", "edit_item", "edit_milestone", "allocate_item",
    "show_capacity", "focus_item",
}


def canvas_action(value) -> dict | None:
    """Reject malformed component messages; record-level validation stays server-side."""
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in _KINDS:
        return None
    if not isinstance(value.get("event_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", value["event_id"]):
        return None
    revision = value.get("source_revision")
    if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision)):
        return None
    identifier = "milestone_id" if kind == "edit_milestone" else "item_id"
    if kind != "show_capacity":
        prefix = "ms" if kind == "edit_milestone" else "rm"
        if not isinstance(value.get(identifier), str) or not re.fullmatch(prefix + r"_[0-9a-f]{32}", value[identifier]):
            return None
    if kind == "schedule_change":
        try:
            parsed = [date.fromisoformat(value[name]) for name in ("old_start", "old_end", "start_date", "end_date")]
        except (KeyError, TypeError, ValueError):
            return None
        if any(not 1900 <= day.year <= 2199 for day in parsed) or parsed[0] > parsed[1] or parsed[2] > parsed[3]:
            return None
        if any(parsed[index].isoformat() != value[name] for index, name in enumerate(("old_start", "old_end", "start_date", "end_date"))):
            return None
    allowed = {"kind", "event_id", "source_revision"}
    if kind != "show_capacity":
        allowed.add(identifier)
    if kind == "schedule_change":
        allowed.update({"old_start", "old_end", "start_date", "end_date"})
    if set(value) - allowed:
        return None
    return deepcopy(value)


def render_roadmap_canvas(
    items: list,
    milestones: list,
    start: date,
    end: date,
    *,
    today: date,
    source_revision,
    palette: dict,
    key: str,
    pending=False,
    ack_event_id="",
    names=None,
    actuals=None,
    view_mode="detail",
    calendar=None,
    selected_id="",
    show_baseline=False,
    editable=False,
    workload=None,
) -> dict | None:
    component = st.components.v2.component(
        "proloag_roadmap_canvas", html=_HTML, css=_CSS, js=_JS
    )
    fields = {
        "id", "title", "start_date", "end_date", "deadline", "planned_minutes",
        "status", "completed", "total", "unit", "progress_kind", "is_archived",
        "predecessor_ids", "baseline_start_date", "baseline_end_date", "memo",
        "project_id", "subject_id", "material_id", "unit_id",
    }
    values = [{name: deepcopy(value) for name, value in row.items()
               if name in fields or name.startswith("view_")} for row in items]
    result = component(
        key=key,
        data={
            "items": values,
            "milestones": [{name: deepcopy(value) for name, value in row.items()
                            if name in {"id", "title", "date", "project_id", "is_archived"}}
                           for row in milestones],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "today": today.isoformat(),
            "source_revision": source_revision,
            "palette": deepcopy(palette),
            "pending": bool(pending),
            "ack_event_id": ack_event_id,
            "names": deepcopy(names or {}),
            "actuals": {name: deepcopy((actuals or {}).get(name, {})) for name in (
                "seconds", "counts", "allocation_counts", "task_counts", "broken_allocation_counts"
            )},
            "view_mode": view_mode if isinstance(view_mode, str) and view_mode in {"detail", "overview"} else "detail",
            "calendar": deepcopy(calendar or {}),
            "selected_id": selected_id,
            "show_baseline": bool(show_baseline),
            "editable": bool(editable),
            "workload": deepcopy(workload),
        },
        on_action=lambda: None,
        width="stretch",
    )
    return canvas_action(result.get("action"))
