// LANTERN v0.12.8 global tactical shell, early stable menu bootstrap
(function(){
  'use strict';
    const params = new URLSearchParams(window.location.search || '');
  const embeddedShellPage = params.has('eei_embed') || params.has('no_shell');
function installProgress(){
    if(window.LanternProgress) return;
    let hideTimer=null;
    let autoActive=0;
    let autoTotal=0;
    let autoDone=0;
    let manual=false;
    function safeDoc(){
      try{ if(!embeddedShellPage && window.parent && window.parent!==window && window.parent.document) return window.parent.document; }catch(_){ }
      return document;
    }
    function findHost(doc){
      return doc.querySelector('.eei-map-mode-strip') || doc.querySelector('header') || doc.querySelector('.hero') || doc.querySelector('#eei-app-shell .eei-shell-top') || doc.body;
    }
    function ensure(){
      const doc=safeDoc();
      let el=doc.getElementById('lanternPageProgress');
      if(!el){
        el=doc.createElement('div');
        el.id='lanternPageProgress';
        el.className='lantern-page-progress';
        el.setAttribute('role','status');
        el.setAttribute('aria-live','polite');
        el.innerHTML='<span class="lantern-page-progress-label">Loading</span><span class="lantern-page-progress-track"><span class="lantern-page-progress-fill"></span></span><span class="lantern-page-progress-pct">0%</span>';
      }
      const host=findHost(doc);
      if(host && el.parentElement!==host) host.appendChild(el);
      return el;
    }
    function setProgress(percent,label,state){
      const el=ensure();
      const pct=Math.max(0,Math.min(100,Math.round(Number(percent)||0)));
      clearTimeout(hideTimer);
      el.classList.add('eei-show');
      el.classList.toggle('eei-done',state==='done');
      el.classList.toggle('eei-error',state==='error');
      const fill=el.querySelector('.lantern-page-progress-fill');
      const pctEl=el.querySelector('.lantern-page-progress-pct');
      const labelEl=el.querySelector('.lantern-page-progress-label');
      if(fill) fill.style.width=pct+'%';
      if(pctEl) pctEl.textContent=pct+'%';
      if(labelEl && label) labelEl.textContent=String(label).replace(/\s+/g,' ').trim().slice(0,72);
      if(state==='done') hideTimer=setTimeout(()=>el.classList.remove('eei-show','eei-done','eei-error'),1400);
      if(state==='error') hideTimer=setTimeout(()=>el.classList.remove('eei-show','eei-error','eei-done'),2800);
    }
    window.LanternProgress={
      begin(label,percent=5){manual=true;setProgress(percent,label||'Loading','active');},
      update(percent,label){setProgress(percent,label||'Loading','active');},
      done(label){setProgress(100,label||'Loaded','done');manual=false;},
      error(label){setProgress(100,label||'Load failed','error');manual=false;},
      note(label){
        const doc=safeDoc();
        const el=doc.getElementById('lanternPageProgress');
        if(!el || !el.classList.contains('eei-show')) return;
        const labelEl=el.querySelector('.lantern-page-progress-label');
        if(labelEl && label) labelEl.textContent=String(label).replace(/\s+/g,' ').trim().slice(0,72);
      }
    };
    const nativeFetch=window.fetch;
    if(!embeddedShellPage && nativeFetch && !nativeFetch.__lanternProgressWrapped){
      const wrapped=function(){
        const args=arguments;
        if(!manual){
          autoActive+=1; autoTotal+=1;
          const pct=Math.min(88,Math.max(6,Math.round(10+(autoDone/Math.max(autoTotal,1))*70)));
          setProgress(pct,'Loading data','active');
        }
        return nativeFetch.apply(this,args).finally(()=>{
          if(manual) return;
          autoActive=Math.max(0,autoActive-1); autoDone+=1;
          if(autoActive===0){
            setProgress(100,'Loaded','done');
            setTimeout(()=>{autoTotal=0;autoDone=0;},350);
          }else{
            const pct=Math.min(92,Math.round(15+(autoDone/Math.max(autoTotal,1))*75));
            setProgress(pct,'Loading data','active');
          }
        });
      };
      wrapped.__lanternProgressWrapped=true;
      window.fetch=wrapped;
    }
  }
  installProgress();
  if(embeddedShellPage) return;
  if(window.__EEI_LANTERN_PLATFORM_SHELL__) return;
  window.__EEI_LANTERN_PLATFORM_SHELL__ = true;
  const VERSION = '0.12.8';
  const VQ = '0122';
  const DEFAULT_NAV = {
    groups:[
      {group:'Home', key:'home', items:[{title:'Platform Home', url:'/app?v=0122', description:'Platform landing page and workflow map.'}]},
      {group:'Mission Context', key:'context', items:[{title:'Mission Context', url:'/context?v=0122', description:'AOI, collections, time window and data quality.'}]},
      {group:'Ingest', key:'ingest', items:[
        {title:'Data Upload', url:'/ingest/upload?v=0122', description:'CSV upload, live-mode entry and future terrain data.'},
        {title:'Live Sensors', url:'/ingest/live-sensors?v=0122', description:'MOTH SDK sessions, raw messages and RF Explorer readiness.'},
        {title:'Import Diagnostics', url:'/ingest/import-diagnostics?v=0122', description:'CSV parser status and ingest troubleshooting.'}
      ]},
      {group:'Reporting', key:'reporting', items:[
        {title:'Flight Safety Brief', url:'/reporting/flight-safety?v=0122', description:'Pilot-facing GNSS/RF burden and clearest observed constellation/band.'},
        {title:'Mission Operations Brief', url:'/reporting/mission-brief?v=0122', description:'Senior/ops readiness summary and caveats.'},
        {title:'J2 Live Report', url:'/reporting/j2?v=0122', description:'OSINT articles, threat actors and source log.'},
        {title:'GNSS Serviceability', url:'/reporting/gnss-serviceability?v=0122', description:'GPS/GNSS serviceability decision support.'},
        {title:'Candidate Site Report', url:'/reporting/candidate?v=0122', description:'End-state antenna/candidate report and PDF entry point.'},
        {title:'Likely Source Report', url:'/reporting/source-location?v=0122', description:'Likely-source output from concurrent suspicious pings.'},
        {title:'Evidence Log', url:'/reporting/evidence-log?v=0122', description:'Source and evidence register.'},
        {title:'Export Pack', url:'/reporting/export?v=0122', description:'Mission report, source log and archive links.'}
      ]},
      {group:'Engineering', key:'engineering', items:[
        {title:'Data Quality Detail', url:'/engineering/data-quality?v=0122', description:'Input confidence, scan coverage and analysis caveats.'},
        {title:'RF Analyst', url:'/engineering/rf?v=0122', description:'Launch windows, L1/L2/L5, spectrum and spikes.'},
        {title:'Spectrum / Spikes', url:'/engineering/spectrum?v=0122', description:'Technical bins, dBm values and abnormal spikes.'},
        {title:'Map / H3 Layers', url:'/engineering/map?v=0122', description:'Map, H3, suitability and confidence layers.'},
        {title:'Candidate Engineering', url:'/engineering/candidates?v=0122', description:'Candidate scoring and evidence drill-down.'},
        {title:'Likely Source Heat Map', url:'/engineering/source-location?v=0122', description:'RSSI confidence heat map for concurrent suspicious pings.'},
        {title:'Pattern of Life', url:'/engineering/pattern-of-life?v=0122', description:'Recurring RF behaviour.'},
      ]},
      {group:'System', key:'system', items:[
        {title:'System Status', url:'/system/status?v=0122', description:'Runtime, database and static status.'},
        {title:'Deploy Check', url:'/system/deploy-check?v=0122', description:'Deployment readiness.'},
        {title:'Logs', url:'/system/logs?v=0122', description:'Local log pointers.'},
        {title:'App Map', url:'/system/app-map?v=0122', description:'Route map and overlap audit.'},
        {title:'Back Office / Legacy', url:'/system/back-office?v=0122', description:'Compatibility aliases, raw payloads and developer support.'}
      ]}
    ]
  };
  let escapeBound = false;
  let resizeBound = false;
  let suppressionStarted = false;
  let started = false;
  let j2MessageBound = false;
  let j2DelegatedBound = false;
  let j2RestoreChecked = false;
  const J2_FLOAT_STORAGE = 'lantern.j2.float.v1';
  const j2Float = {
    open:false, expanded:false, minimized:false, paused:false,
    aoi:'', dateFrom:'', articles:[], index:0, message:'', generatedUtc:'',
    liveCount:0, status:'idle', loading:false, loadedAt:0,
    left:null, top:null, timer:null
  };

  function path(){return window.location.pathname.replace(/\/+$/,'') || '/';}
  function groupForPath(){const p=path(); if(p==='/app'||p==='/') return 'home'; if(p==='/context') return 'context'; if(p.startsWith('/ingest')) return 'ingest'; if(p.startsWith('/reporting')||p==='/lantern'||p.includes('mission_brief')||p.includes('j2_report')) return 'reporting'; if(p==='/engineering/api-viewer') return 'system'; if(p.startsWith('/engineering')||p.includes('launch_analysis')||p.includes('data_quality')||p.endsWith('/index.html')) return 'engineering'; if(p.startsWith('/system')||p.startsWith('/api/platform')) return 'system'; return document.body.dataset.navGroup || 'home';}
  function isMapWorkspace(){const p=path(); return p==='/' || p==='/engineering/map' || p==='/engineering/candidates' || p.endsWith('/index.html') || location.hash==='#candidates';}
  function active(url){try{const u=new URL(url,location.origin);const p=u.pathname.replace(/\/+$/,'')||'/';const c=path(); if(p==='/'&&(c==='/'||c.endsWith('/index.html'))) return true; if(c==='/lantern'&&p==='/reporting/flight-safety') return true; if(c.includes('mission_brief')&&p==='/reporting/mission-brief') return true; if(c.includes('j2_report')&&p==='/reporting/j2') return true; if(c.includes('launch_analysis')&&(p==='/engineering/rf'||p==='/engineering/spectrum')) return true; if(c.includes('data_quality')&&p==='/engineering/data-quality') return true; if(c==='/engineering/api-viewer'&&p==='/system/back-office') return true; if((c==='/engineering/map'||c==='/engineering/candidates')&&p===c) return true; return p===c;}catch(_){return false;}}
  function esc(x){return String(x==null?'':x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function normalize(payload){if(!payload||!Array.isArray(payload.groups)) return DEFAULT_NAV.groups; return payload.groups.map(g=>({group:g.group||'Menu', key:g.key||String(g.group||'').toLowerCase(), items:Array.isArray(g.items)?g.items:[]})).filter(g=>g.items.length);}
  async function getJson(url){const r=await fetch(url,{cache:'no-store'}); if(!r.ok) throw new Error(url+' '+r.status); return r.json();}
  function statusClass(status){if(status==='ok'||status==='ready') return 'good'; if(status==='error'||status==='not_ready') return 'bad'; return 'check';}
  function rewriteVersion(url){try{const u=new URL(url, location.origin); if(u.searchParams.has('v')) u.searchParams.set('v', VQ); return u.pathname + u.search + u.hash;}catch(_){return url;}}
  function j2DateDaysAgo(days){const d=new Date();d.setUTCDate(d.getUTCDate()-Number(days||0));return d.toISOString().slice(0,10);}
  function j2ReadStored(){
    try{
      const raw=localStorage.getItem(J2_FLOAT_STORAGE);
      const data=raw?JSON.parse(raw):null;
      return data&&typeof data==='object'?data:{};
    }catch(_){return {};}
  }
  function j2Store(){
    try{
      localStorage.setItem(J2_FLOAT_STORAGE, JSON.stringify({
        open:!!j2Float.open, expanded:!!j2Float.expanded, minimized:!!j2Float.minimized,
        aoi:j2Float.aoi||'', dateFrom:j2Float.dateFrom||'', left:j2Float.left, top:j2Float.top
      }));
    }catch(_){}
  }
  function j2SafeUrl(value){
    const text=String(value||'').trim();
    if(/^data:image\/(?:svg\+xml|png|jpeg|webp);/i.test(text)) return text;
    try{const u=new URL(text, location.href); return (u.protocol==='http:'||u.protocol==='https:')?u.href:'';}catch(_){return '';}
  }
  function j2Initials(article){
    const source=String(article&&article.source||article&&article.title||'J2').replace(/[^A-Za-z0-9 ]+/g,' ').trim();
    const words=source.split(/\s+/).filter(Boolean);
    return (words.length>1?(words[0][0]+words[1][0]):(words[0]||'J2').slice(0,2)).toUpperCase();
  }
  function j2FallbackMedia(article){
    const label=j2Initials(article);
    const source=String(article&&article.source||'OSINT').replace(/[<>&"']/g,'').slice(0,28);
    const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180"><defs><linearGradient id="g" x1="0" x2="1" y1="0" y2="1"><stop stop-color="#163d2a"/><stop offset="1" stop-color="#07110d"/></linearGradient></defs><rect width="320" height="180" fill="url(#g)"/><circle cx="256" cy="42" r="54" fill="#6ee7a8" opacity=".13"/><rect x="18" y="20" width="284" height="140" rx="14" fill="none" stroke="#6ee7a8" stroke-opacity=".38"/><text x="32" y="96" fill="#edf3ef" font-family="Arial,Helvetica,sans-serif" font-size="42" font-weight="800">${label}</text><text x="34" y="124" fill="#b8c3bd" font-family="Arial,Helvetica,sans-serif" font-size="15">${source}</text></svg>`;
    return 'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg);
  }
  function j2ArticleItems(){
    const seen=new Set();
    return (Array.isArray(j2Float.articles)?j2Float.articles:[]).filter(a=>{
      const key=String((a&&a.url)||'')+'|'+String((a&&a.title)||'');
      if(!key.trim()||seen.has(key)) return false;
      seen.add(key);
      return true;
    }).slice(0,24);
  }
  function j2MediaHtml(article){
    const image=j2SafeUrl(article&&article.image_url||article&&article.thumbnail_url);
    const icon=j2SafeUrl(article&&article.source_icon_url);
    const fallback=j2FallbackMedia(article);
    const src=image||icon||fallback;
    return `<div class="eei-j2-media ${image?'':(icon?'eei-icon-only':'')}"><img src="${esc(src)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.onerror=null;this.src='${esc(fallback)}';this.closest('.eei-j2-media').classList.remove('eei-icon-only');" /></div>`;
  }
  function j2ArticleHtml(article, feature=false){
    if(!article) return '<div class="eei-j2-empty">No OSINT articles loaded.</div>';
    const url=j2SafeUrl(article.url)||'#';
    const title=article.title||article.source||'Source item';
    const summary=article.summary||article.query||'';
    const meta=[article.source||'Source',article.published_utc||'no date',article.category||'OSINT'].filter(Boolean).join(' | ');
    const live=article.live?'<span class="eei-j2-pill eei-live">LIVE</span>':'<span class="eei-j2-pill">SOURCE</span>';
    const score=article.relevance_score!=null?`<span class="eei-j2-pill">Score ${esc(article.relevance_score)}</span>`:'';
    return `<article class="eei-j2-article ${feature?'eei-feature':''}">${j2MediaHtml(article)}<div class="eei-j2-article-text"><a class="eei-j2-title" href="${esc(url)}" target="_blank" rel="noreferrer noopener">${esc(title)}</a><div class="eei-j2-meta">${esc(meta)} ${live} ${score}</div><div class="eei-j2-summary">${esc(summary)}</div></div></article>`;
  }
  function j2ApplyPosition(panel){
    if(!panel) return;
    if(Number.isFinite(j2Float.left)&&Number.isFinite(j2Float.top)){
      panel.style.left=Math.max(8,Math.min(window.innerWidth-panel.offsetWidth-8,j2Float.left))+'px';
      panel.style.top=Math.max(8,Math.min(window.innerHeight-panel.offsetHeight-8,j2Float.top))+'px';
      panel.style.right='auto';
      panel.style.bottom='auto';
    }else{
      panel.style.left='';
      panel.style.top='';
      panel.style.right='';
      panel.style.bottom='';
    }
  }
  function ensureJ2Float(){
    let panel=document.getElementById('eei-j2-float');
    if(!panel){
      panel=document.createElement('aside');
      panel.id='eei-j2-float';
      panel.setAttribute('aria-live','polite');
      panel.innerHTML=`<div class="eei-j2-head"><div class="eei-j2-heading"><b>J2 OSINT</b><span id="eeiJ2FloatStatus">Idle</span></div><div class="eei-j2-head-actions"><button type="button" data-j2-act="refresh" title="Refresh OSINT">Refresh</button><button type="button" data-j2-act="next" title="Next article">Next</button><button type="button" data-j2-act="expand" title="Expand panel">Expand</button><button type="button" data-j2-act="min" title="Minimize panel">Min</button><button type="button" data-j2-act="close" title="Close panel">Close</button></div></div><div class="eei-j2-controls"><label>AOI <input id="eeiJ2FloatAoi" type="text" /></label><label>Date from <input id="eeiJ2FloatDate" type="date" /></label><button type="button" data-j2-act="last30">30d</button><button type="button" data-j2-act="all">All</button><a href="/reporting/j2?v=0122">Open J2</a></div><div class="eei-j2-body" id="eeiJ2FloatBody"></div><div class="eei-j2-foot" id="eeiJ2FloatFoot">Not loaded</div>`;
      document.body.appendChild(panel);
      wireJ2FloatPanel(panel);
    }
    return panel;
  }
  function wireJ2FloatPanel(panel){
    if(panel.__eeiJ2Wired) return;
    panel.__eeiJ2Wired=true;
    panel.addEventListener('click',ev=>{
      const target=ev.target&&ev.target.closest?ev.target.closest('[data-j2-act],[data-j2-pick]'):null;
      if(!target) return;
      const pick=target.getAttribute('data-j2-pick');
      if(pick!=null){j2Float.index=Number(pick)||0;j2RenderFloat();return;}
      const act=target.getAttribute('data-j2-act');
      if(act==='refresh') j2LoadFloat(true);
      if(act==='next') j2NextFloat();
      if(act==='expand'){j2Float.expanded=!j2Float.expanded;j2Float.minimized=false;j2Store();j2RenderFloat();}
      if(act==='min'){j2Float.minimized=!j2Float.minimized;j2Store();j2RenderFloat();}
      if(act==='close') j2CloseFloat();
      if(act==='last30'){j2Float.dateFrom=j2DateDaysAgo(30);j2Store();j2LoadFloat(true);}
      if(act==='all'){j2Float.dateFrom='';j2Store();j2LoadFloat(true);}
    });
    panel.addEventListener('change',ev=>{
      const t=ev.target;
      if(t&&t.id==='eeiJ2FloatAoi'){j2Float.aoi=String(t.value||'').trim();j2Store();j2LoadFloat(true);}
      if(t&&t.id==='eeiJ2FloatDate'){j2Float.dateFrom=String(t.value||'').trim();j2Store();j2LoadFloat(true);}
    });
    const head=panel.querySelector('.eei-j2-head');
    if(head){
      head.addEventListener('pointerdown',ev=>{
        if(ev.button!==0 || (ev.target&&ev.target.closest&&ev.target.closest('button,a,input,label'))) return;
        const rect=panel.getBoundingClientRect();
        const startX=ev.clientX, startY=ev.clientY, startL=rect.left, startT=rect.top;
        const move=moveEv=>{
          j2Float.left=startL+(moveEv.clientX-startX);
          j2Float.top=startT+(moveEv.clientY-startY);
          j2ApplyPosition(panel);
        };
        const up=()=>{document.removeEventListener('pointermove',move);j2Store();};
        document.addEventListener('pointermove',move);
        document.addEventListener('pointerup',up,{once:true});
      });
    }
  }
  function j2RenderFloat(){
    const panel=ensureJ2Float();
    panel.classList.toggle('eei-show',!!j2Float.open);
    panel.classList.toggle('eei-expanded',!!j2Float.expanded);
    panel.classList.toggle('eei-minimized',!!j2Float.minimized);
    const aoi=panel.querySelector('#eeiJ2FloatAoi');
    const date=panel.querySelector('#eeiJ2FloatDate');
    if(aoi&&document.activeElement!==aoi) aoi.value=j2Float.aoi||'';
    if(date&&document.activeElement!==date) date.value=j2Float.dateFrom||'';
    const expandBtn=panel.querySelector('[data-j2-act="expand"]');
    const minBtn=panel.querySelector('[data-j2-act="min"]');
    if(expandBtn) expandBtn.textContent=j2Float.expanded?'Compact':'Expand';
    if(minBtn) minBtn.textContent=j2Float.minimized?'Restore':'Min';
    const status=panel.querySelector('#eeiJ2FloatStatus');
    if(status) status.textContent=j2Float.loading?'Loading':(j2Float.status||'Idle');
    const body=panel.querySelector('#eeiJ2FloatBody');
    const foot=panel.querySelector('#eeiJ2FloatFoot');
    const items=j2ArticleItems();
    if(body){
      if(j2Float.loading && !items.length){
        body.innerHTML='<div class="eei-j2-empty">Loading live OSINT...</div>';
      }else if(!items.length){
        body.innerHTML='<div class="eei-j2-empty">No OSINT articles loaded. Refresh to check the feed.</div>';
      }else{
        if(j2Float.index>=items.length) j2Float.index=0;
        const featured=j2ArticleHtml(items[j2Float.index],true);
        const rows=items.map((a,i)=>`<button type="button" class="eei-j2-row ${i===j2Float.index?'eei-active':''}" data-j2-pick="${i}"><span>${esc(a.title||a.source||'Source item')}</span><small>${esc(a.source||'Source')} | ${esc(a.published_utc||'no date')}</small></button>`).join('');
        body.innerHTML=`${featured}<div class="eei-j2-list">${rows}</div>`;
      }
    }
    if(foot) foot.textContent=items.length?`Item ${Math.min(j2Float.index+1,items.length)}/${items.length} | live ${j2Float.liveCount||0} | from ${j2Float.dateFrom||'all dates'}`:(j2Float.message||'Feed not loaded');
    j2ApplyPosition(panel);
    syncJ2ShellButton();
  }
  async function j2ResolveAoi(){
    if(j2Float.aoi) return j2Float.aoi;
    try{
      const ctx=await getJson('/api/platform/mission-context');
      j2Float.aoi=ctx.aoi_query||ctx.aoi||'Lincoln, UK';
    }catch(_){j2Float.aoi='Lincoln, UK';}
    return j2Float.aoi;
  }
  async function j2LoadFloat(force=false){
    if(j2Float.loading) return;
    j2Float.loading=true;
    j2Float.status='Loading';
    j2RenderFloat();
    try{
      const aoi=await j2ResolveAoi();
      if(j2Float.dateFrom===null || j2Float.dateFrom===undefined) j2Float.dateFrom='';
      const p=new URLSearchParams({aoi:aoi||'Lincoln, UK',live:'true',force:force?'true':'false',limit:'24'});
      if(j2Float.dateFrom) p.set('date_from',j2Float.dateFrom);
      const data=await getJson('/api/j2/news?'+p.toString());
      j2Float.articles=Array.isArray(data.articles)?data.articles:[];
      j2Float.generatedUtc=data.generated_utc||'';
      j2Float.message=data.message||'';
      j2Float.liveCount=Number(data.live_count||0);
      j2Float.status=j2Float.liveCount?'Live':'Source register';
      j2Float.loadedAt=Date.now();
      j2Float.index=0;
    }catch(err){
      j2Float.status='Feed error';
      j2Float.message=String(err&&err.message||err||'OSINT feed error');
    }finally{
      j2Float.loading=false;
      j2Store();
      j2RenderFloat();
    }
  }
  function j2OpenFloat(opts={}){
    Object.assign(j2Float,j2ReadStored(),opts||{});
    if(!j2Float.dateFrom && opts.date_from) j2Float.dateFrom=String(opts.date_from||'');
    if(!j2Float.dateFrom && !opts.date_from && !j2Float.open) j2Float.dateFrom=j2DateDaysAgo(30);
    if(opts.aoi) j2Float.aoi=String(opts.aoi||'').trim();
    if(opts.date_from!==undefined) j2Float.dateFrom=String(opts.date_from||'').trim();
    j2Float.open=true;
    j2Float.minimized=false;
    j2Store();
    j2RenderFloat();
    const stale=!j2Float.loadedAt || Date.now()-j2Float.loadedAt>10*60*1000;
    if(!j2ArticleItems().length || stale || opts.force) j2LoadFloat(!!opts.force);
    j2StartFloatTimer();
  }
  function j2CloseFloat(){
    j2Float.open=false;
    j2Store();
    const panel=document.getElementById('eei-j2-float');
    if(panel) panel.classList.remove('eei-show');
    syncJ2ShellButton();
  }
  function j2ToggleFloat(){
    const visible=!!document.querySelector('#eei-j2-float.eei-show');
    if(j2Float.open&&visible) j2CloseFloat();
    else j2OpenFloat();
  }
  function j2NextFloat(){
    const items=j2ArticleItems();
    if(!items.length) return;
    j2Float.index=(j2Float.index+1)%items.length;
    j2RenderFloat();
  }
  function j2StartFloatTimer(){
    if(j2Float.timer) return;
    j2Float.timer=setInterval(()=>{if(j2Float.open&&!j2Float.minimized&&!j2Float.paused) j2NextFloat();},12000);
  }
  function syncJ2ShellButton(root=document){
    try{(root||document).querySelectorAll('.eei-osint-toggle').forEach(btn=>btn.classList.toggle('eei-active',!!j2Float.open));}catch(_){}
  }
  function restoreJ2Float(){
    if(j2RestoreChecked) return;
    j2RestoreChecked=true;
    Object.assign(j2Float,j2ReadStored());
    if(j2Float.open) setTimeout(()=>j2OpenFloat({}),80);
  }
  function handleJ2FloatMessage(event){
    try{
      const data=event&&event.data?event.data:(event&&event.detail?event.detail:null);
      if(event&&event.origin&&event.origin!==location.origin) return;
      if(!data||data.type!=='lantern:j2:float') return;
      if(data.action==='close') j2CloseFloat();
      else j2OpenFloat({aoi:data.aoi||j2Float.aoi,date_from:data.date_from,force:true});
    }catch(_){}
  }
  function textOf(el){return (el && (el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('href') || '') || '').replace(/\s+/g,' ').trim();}
  function hideDeprecatedFlowButtons(root=document){
    const badText = /\bLANTERN\s*FLOW\b|\bLANTERNFLOW\b|\bFLOW\s*BUTTON\b/i;
    const badHref = /lantern[_-]?flow|flow\.html|\/flow(?:\?|#|$)/i;
    root.querySelectorAll('a,button,[role="button"]').forEach(el=>{
      const t=textOf(el); const href=el.getAttribute('href')||'';
      if(badText.test(t) || badHref.test(href)){
        el.style.setProperty('display','none','important');
        el.setAttribute('aria-hidden','true');
        el.setAttribute('data-eei-hidden','legacy-flow');
      }
    });
  }
  function setShellHeight(){const shell=document.getElementById('eei-app-shell'); if(!shell) return; const h=Math.ceil(shell.getBoundingClientRect().height || 0); document.documentElement.style.setProperty('--eei-shell-h', h+'px'); document.body.style.setProperty('--eei-shell-h', h+'px');}
  function startFlowSuppression(){
    hideDeprecatedFlowButtons(document);
    let ticks=0;
    const timer=setInterval(()=>{hideDeprecatedFlowButtons(document); if(++ticks>30) clearInterval(timer);}, 500);
    try{if(document.documentElement && document.documentElement.nodeType===1){new MutationObserver(()=>hideDeprecatedFlowButtons(document)).observe(document.documentElement,{childList:true,subtree:true,characterData:true});}}catch(_){}
  }
  function shellMarkup(groups, health, ctx){
    const activeGroup=groupForPath();
    const hstatus=(health&&health.status)||'check';
    const sClass=statusClass(hstatus);
    const sText=sClass==='good'?'API online':sClass==='bad'?'API fault':'Check status';
    groups = groups.map(g=>({group:g.group,key:g.key,items:(g.items||[]).map(i=>Object.assign({},i,{url:rewriteVersion(i.url||'')}))}));
    const nav=groups.map(group=>{
      const key=group.key||String(group.group||'').toLowerCase();
      const items=(group.items||[]).map(item=>`<a class="eei-link${active(item.url)?' eei-active':''}" href="${esc(item.url)}"><span class="eei-link-title">${esc(item.title)}</span><span class="eei-link-desc">${esc(item.description||'')}</span></a>`).join('');
      return `<div class="eei-group${key===activeGroup?' eei-active':''}"><button type="button" aria-haspopup="true">${esc(group.group)}</button><div class="eei-menu">${items}</div></div>`;
    }).join('');
    const group = groups.find(g=>(g.key||String(g.group||'').toLowerCase())===activeGroup) || groups[0];
    const secondary = `<div class="eei-secondary-row eei-show"><span class="eei-secondary-label">${esc(group.group||'Menu')}</span>${(group.items||[]).map(i=>`<a class="eei-secondary-link eei-${activeGroup}${active(i.url)?' eei-active':''}" href="${esc(i.url)}">${esc(i.title)}</a>`).join('')}</div>`;
    const q=(ctx&&ctx.data_quality&&ctx.data_quality.level)||'NO DATA';
    const qClass=q==='GOOD'?'good':q==='CHECK'||q==='MEDIUM'?'check':q==='LOW'?'low':'no-data';
    const col=ctx&&ctx.collections?ctx.collections:{};
    const time=ctx&&ctx.time_window?ctx.time_window:{};
    const context=`<div class="eei-context-row"><span class="eei-context-pill"><b>AOI</b> ${esc((ctx&&ctx.aoi)||'not set')}</span><span class="eei-context-pill"><b>Collections</b> ${esc(col.selected||'all loaded')} (${esc(col.count??'-')})</span><span class="eei-context-pill"><b>Time</b> ${esc(time.first_timestamp_utc||'start')} -> ${esc(time.last_timestamp_utc||'end')}</span><span class="eei-context-pill ${qClass}"><b>Data</b> ${esc(q)}</span><span class="eei-context-pill"><b>Spike</b> ${esc((ctx&&ctx.rf_threshold)||'-60 dBm')}</span><span class="eei-context-pill"><b>Mode</b> ${esc(activeGroup)}</span><button class="eei-context-pill eei-context-toggle" type="button" title="Collapse/expand mission context">CTX</button></div>`;
    return `<div class="eei-shell-top"><div class="eei-brand"><a class="eei-brand-mark" href="/app?v=${VQ}" title="LANTERN Home" aria-label="LANTERN Home"><img src="/static/eei_tactical_eagle.svg?v=0131" alt="" aria-hidden="true"></a><div class="eei-brand-text"><a href="/app?v=${VQ}">LANTERN</a><span>Launch Analysis and Network Telemetry Evaluation for RF Navigation | v${VERSION}</span></div></div><button class="eei-toggle" type="button" aria-label="Open platform menu">Menu</button><nav class="eei-primary-nav" aria-label="LANTERN platform navigation">${nav}</nav><div class="eei-actions"><button class="eei-osint-toggle" type="button" title="Open floating J2 OSINT articles" aria-label="Open floating J2 OSINT articles">J2 OSINT</button><span class="eei-status"><span class="eei-dot ${sClass}"></span>${esc(sText)}</span></div></div>${context}${secondary}`;
  }
  function wireShell(shell){
    const toggle=shell.querySelector('.eei-toggle');
    if(toggle) toggle.addEventListener('click',()=>{shell.classList.toggle('eei-mobile-open'); setTimeout(setShellHeight,0);});
    const ctxToggle=shell.querySelector('.eei-context-toggle');
    if(ctxToggle) ctxToggle.addEventListener('click',()=>{document.body.classList.toggle('eei-context-collapsed'); setShellHeight();});
    shell.querySelectorAll('.eei-group>button').forEach(btn=>btn.addEventListener('click',()=>{const g=btn.closest('.eei-group'); shell.querySelectorAll('.eei-group.eei-open').forEach(x=>{if(x!==g) x.classList.remove('eei-open')}); if(g) g.classList.toggle('eei-open');}));
    if(!escapeBound){document.addEventListener('keydown',ev=>{const current=document.getElementById('eei-app-shell'); if(ev.key==='Escape'&&current){current.classList.remove('eei-mobile-open'); current.querySelectorAll('.eei-group.eei-open').forEach(g=>g.classList.remove('eei-open'));}}); escapeBound=true;}
    if(!resizeBound){window.addEventListener('resize',setShellHeight,{passive:true}); resizeBound=true;}
    if(!j2DelegatedBound){document.addEventListener('click',ev=>{const btn=ev.target&&ev.target.closest?ev.target.closest('.eei-osint-toggle'):null;if(!btn)return;ev.preventDefault();j2ToggleFloat();});j2DelegatedBound=true;}
    if(!j2MessageBound){window.addEventListener('message',handleJ2FloatMessage);window.addEventListener('lantern:j2:float',handleJ2FloatMessage);j2MessageBound=true;}
    restoreJ2Float();
    syncJ2ShellButton(shell);
  }
  function render(groups, health, ctx){
    if(!document.body || (document.body.dataset && document.body.dataset.noPlatformShell==='true')) return;
    const activeGroup=groupForPath();
    let shell=document.getElementById('eei-app-shell');
    if(!shell){
      shell=document.createElement('div');
      shell.id='eei-app-shell';
      shell.setAttribute('data-eei-shell','stable');
      document.body.insertBefore(shell, document.body.firstChild);
    }
    shell.innerHTML=shellMarkup(groups, health, ctx);
    document.body.classList.add('eei-shell-mounted');
    if(isMapWorkspace()) document.body.classList.add('eei-map-workspace');
    const pageKey = path().replace(/^\//,'').replace(/\//g,'_') || 'home';
    document.body.dataset.lanternPage = document.body.dataset.lanternPage || pageKey;
    document.body.dataset.navGroup = activeGroup;
    wireShell(shell);
    if(!suppressionStarted){startFlowSuppression(); suppressionStarted=true;}
    setShellHeight();
    setTimeout(setShellHeight,60);
    setTimeout(setShellHeight,250);
  }
  function start(){
    if(started) return;
    started = true;
    if(document.body && document.body.dataset && document.body.dataset.noPlatformShell==='true') return;
    render(DEFAULT_NAV.groups,{status:'check'},null);
    Promise.allSettled([getJson('/api/platform/navigation'),getJson('/api/platform/health'),getJson('/api/platform/mission-context')]).then(r=>{
      render(normalize(r[0].status==='fulfilled'?r[0].value:null), r[1].status==='fulfilled'?r[1].value:{status:'check'}, r[2].status==='fulfilled'?r[2].value:null);
      setTimeout(setShellHeight,600);
    }).catch(()=>render(DEFAULT_NAV.groups,{status:'check'},null));
  }
  if(document.body) start(); else document.addEventListener('DOMContentLoaded',start,{once:true});
})();
