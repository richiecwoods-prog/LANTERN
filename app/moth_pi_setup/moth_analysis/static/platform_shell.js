// EEI LANTERN v0.12 global platform navigation shell
(function(){
  'use strict';
  if(window.__EEI_LANTERN_PLATFORM_SHELL__) return;
  window.__EEI_LANTERN_PLATFORM_SHELL__ = true;
  const VERSION = '0.12.0';
  const DEFAULT_NAV = {
    groups:[
      {group:'Home', key:'home', items:[{title:'Platform Home', url:'/app?v=012', description:'Platform landing page and workflow map.'}]},
      {group:'Mission Context', key:'context', items:[{title:'Mission Context', url:'/context?v=012', description:'AOI, collections, time window and data quality.'}]},
      {group:'Reporting', key:'reporting', items:[
        {title:'Flight Safety Brief', url:'/reporting/flight-safety?v=012', description:'Pilot-facing GNSS/RF burden and clearest observed constellation/band.'},
        {title:'Mission Operations Brief', url:'/reporting/mission-brief?v=012', description:'Senior/ops readiness summary and caveats.'},
        {title:'J2 Live Report', url:'/reporting/j2?v=012', description:'OSINT articles, threat actors and source log.'},
        {title:'GNSS Serviceability', url:'/reporting/gnss-serviceability?v=012', description:'GPS/GNSS serviceability decision support.'},
        {title:'Candidate Site Report', url:'/reporting/candidate?v=012', description:'End-state candidate report and PDF entry point.'},
        {title:'Export Pack', url:'/reporting/export?v=012', description:'Mission report, source log and archive links.'}
      ]},
      {group:'Engineering', key:'engineering', items:[
        {title:'Data Quality Detail', url:'/engineering/data-quality?v=012', description:'Reject/flag detail and scan quality.'},
        {title:'RF Analyst', url:'/engineering/rf?v=012', description:'Launch windows, L1/L2/L5, spectrum and spikes.'},
        {title:'Map / H3 Layers', url:'/engineering/map?v=012', description:'Map, H3, suitability and confidence layers.'},
        {title:'Candidate Engineering', url:'/engineering/candidates?v=012', description:'Candidate scoring and evidence drill-down.'},
        {title:'API Payload Viewer', url:'/engineering/api-viewer?v=012', description:'Inspect JSON endpoints.'}
      ]},
      {group:'System', key:'system', items:[
        {title:'System Status', url:'/system/status?v=012', description:'Runtime, database and static status.'},
        {title:'Deploy Check', url:'/system/deploy-check?v=012', description:'Deployment readiness.'},
        {title:'App Map', url:'/system/app-map?v=012', description:'Route map and overlap audit.'}
      ]}
    ]
  };
  function path(){return window.location.pathname.replace(/\/+$/,'') || '/';}
  function groupForPath(){const p=path(); if(p==='/app'||p==='/') return 'home'; if(p==='/context') return 'context'; if(p.startsWith('/reporting')||p==='/lantern'||p.includes('mission_brief')||p.includes('j2_report')) return 'reporting'; if(p.startsWith('/engineering')||p.includes('launch_analysis')||p.includes('data_quality')||p.endsWith('/index.html')) return 'engineering'; if(p.startsWith('/system')||p.startsWith('/api/platform')) return 'system'; return document.body.dataset.navGroup || 'home';}
  function active(url){try{const u=new URL(url,location.origin);const p=u.pathname.replace(/\/+$/,'')||'/';const c=path(); if(p==='/'&&(c==='/'||c.endsWith('/index.html'))) return true; if(c==='/lantern'&&p==='/reporting/flight-safety') return true; if(c.includes('mission_brief')&&p==='/reporting/mission-brief') return true; if(c.includes('j2_report')&&p==='/reporting/j2') return true; if(c.includes('launch_analysis')&&(p==='/engineering/rf'||p==='/engineering/spectrum')) return true; if(c.includes('data_quality')&&p==='/engineering/data-quality') return true; return p===c;}catch(_){return false;}}
  function esc(x){return String(x==null?'':x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function normalize(payload){if(!payload||!Array.isArray(payload.groups)) return DEFAULT_NAV.groups; return payload.groups.map(g=>({group:g.group||'Menu', key:g.key||String(g.group||'').toLowerCase(), items:Array.isArray(g.items)?g.items:[]})).filter(g=>g.items.length);}
  async function getJson(url){const r=await fetch(url,{cache:'no-store'}); if(!r.ok) throw new Error(url+' '+r.status); return r.json();}
  function statusClass(status){if(status==='ok'||status==='ready') return 'good'; if(status==='error'||status==='not_ready') return 'bad'; return 'check';}
  function render(groups, health, ctx){if(document.getElementById('eei-app-shell')) return; const activeGroup=groupForPath(); const hstatus=(health&&health.status)||'check'; const sClass=statusClass(hstatus); const sText=sClass==='good'?'API online':sClass==='bad'?'API fault':'Check status';
    const nav=groups.map(group=>{const key=group.key||String(group.group||'').toLowerCase(); const items=(group.items||[]).map(item=>`<a class="eei-link${active(item.url)?' eei-active':''}" href="${esc(item.url)}"><span class="eei-link-title">${esc(item.title)}${item.legacy_url?'<span class="eei-legacy">alias</span>':''}</span><span class="eei-link-desc">${esc(item.description||'')}</span></a>`).join(''); return `<div class="eei-group${key===activeGroup?' eei-active':''}"><button type="button" aria-haspopup="true">${esc(group.group)}</button><div class="eei-menu">${items}</div></div>`;}).join('');
    const group = groups.find(g=>(g.key||String(g.group||'').toLowerCase())===activeGroup); const secondary = group && ['reporting','engineering','system'].includes(activeGroup) ? `<div class="eei-secondary-row eei-show"><span class="eei-secondary-label">${esc(group.group)}</span>${(group.items||[]).map(i=>`<a class="eei-secondary-link eei-${activeGroup}${active(i.url)?' eei-active':''}" href="${esc(i.url)}">${esc(i.title)}</a>`).join('')}</div>` : '<div class="eei-secondary-row"></div>';
    const q=(ctx&&ctx.data_quality&&ctx.data_quality.level)||'NO DATA'; const qClass=q==='GOOD'?'good':q==='CHECK'||q==='MEDIUM'?'check':q==='LOW'?'low':'no-data'; const col=ctx&&ctx.collections?ctx.collections:{}; const time=ctx&&ctx.time_window?ctx.time_window:{};
    const context=`<div class="eei-context-row"><span class="eei-context-pill"><b>AOI</b> ${esc((ctx&&ctx.aoi)||'not set')}</span><span class="eei-context-pill"><b>Collections</b> ${esc(col.selected||'all loaded')} (${esc(col.count??'—')})</span><span class="eei-context-pill"><b>Time</b> ${esc(time.first_timestamp_utc||'start')} → ${esc(time.last_timestamp_utc||'end')}</span><span class="eei-context-pill ${qClass}"><b>Data</b> ${esc(q)}</span><span class="eei-context-pill"><b>Spike</b> ${esc((ctx&&ctx.rf_threshold)||'-60 dBm')}</span><span class="eei-context-pill"><b>Mode</b> ${esc(activeGroup)}</span></div>`;
    const shell=document.createElement('div'); shell.id='eei-app-shell'; shell.innerHTML=`<div class="eei-shell-top"><div class="eei-brand"><a href="/app?v=012">EEI LANTERN</a><span>Launch Analysis and Network Telemetry Evaluation for RF Navigation | v${VERSION}</span></div><button class="eei-toggle" type="button" aria-label="Open platform menu">Menu</button><nav class="eei-primary-nav" aria-label="EEI LANTERN platform navigation">${nav}</nav><div class="eei-actions"><span class="eei-status"><span class="eei-dot ${sClass}"></span>${esc(sText)}</span><a class="eei-action" href="/app?v=012">Home</a></div></div>${context}${secondary}`; document.body.insertBefore(shell,document.body.firstChild); document.body.classList.add('eei-shell-mounted');
    const toggle=shell.querySelector('.eei-toggle'); if(toggle) toggle.addEventListener('click',()=>shell.classList.toggle('eei-mobile-open')); shell.querySelectorAll('.eei-group>button').forEach(btn=>btn.addEventListener('click',()=>{const g=btn.closest('.eei-group'); shell.querySelectorAll('.eei-group.eei-open').forEach(x=>{if(x!==g) x.classList.remove('eei-open')}); if(g) g.classList.toggle('eei-open');})); document.addEventListener('keydown',ev=>{if(ev.key==='Escape'){shell.classList.remove('eei-mobile-open'); shell.querySelectorAll('.eei-group.eei-open').forEach(g=>g.classList.remove('eei-open'));}});
  }
  function start(){if(document.body && document.body.dataset && document.body.dataset.noPlatformShell==='true') return; Promise.allSettled([getJson('/api/platform/navigation'),getJson('/api/platform/health'),getJson('/api/platform/mission-context')]).then(r=>{render(normalize(r[0].status==='fulfilled'?r[0].value:null), r[1].status==='fulfilled'?r[1].value:{status:'check'}, r[2].status==='fulfilled'?r[2].value:null);}).catch(()=>render(DEFAULT_NAV.groups,{status:'check'},null));}
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',start); else start();
})();
