// EEI LANTERN v0.11.1 global platform navigation shell
(function () {
  'use strict';

  // EEI-LANTERN-J2-NO-SHELL-GUARD: Some purpose-built pages, including the J2 live report,
  // carry their own single-page command header. Do not mount the global shell there.
  if (document.querySelector('meta[name="eei-no-platform-shell"]') ||
      (document.body && document.body.dataset && document.body.dataset.noPlatformShell === 'true') ||
      window.location.pathname.replace(/\/+$/, '').endsWith('/static/j2_report.html')) return;


  if (window.__EEI_LANTERN_PLATFORM_SHELL__) return;
  window.__EEI_LANTERN_PLATFORM_SHELL__ = true;

  const VERSION = '0.11.1';
  const DEFAULT_GROUPS = [
    {
      group: 'Briefing',
      items: [
        { title: 'Platform Home', url: '/app?v=0111', description: 'Role-based start page and deployment status.' },
        { title: 'Flight Safety Brief', url: '/lantern?v=0111', description: 'Pilot and flight-safety GNSS clearance view.' },
        { title: 'Mission Brief', url: '/static/mission_brief.html?v=091', description: 'RF readiness and mission summary.' },
        { title: 'J2 Article Rotator', url: '/static/j2_report.html?v=113', description: 'J2/JSP 101 report controls and preview.' },
        { title: 'JSP 101 Report', url: '/api/mission/report-jsp101.html', description: 'Direct printable structured RF mission report.' }
      ]
    },
    {
      group: 'Operations',
      items: [
        { title: 'Map / Candidates', url: '/?v=0111', description: 'Upload scans, view map, add and score antenna candidates.' },
        { title: 'Data Quality', url: '/static/data_quality.html?v=080', description: 'Check scan quality before briefing outputs.' },
        { title: 'Simple Briefing Cards', url: '/static/briefing.html?v=080', description: 'Plain-English decision cards.' },
        { title: 'J2 Article Rotator', url: '/static/j2_report.html?v=113', description: 'Cycle platform pages on a briefing display.' }
      ]
    },
    {
      group: 'Analyst',
      items: [
        { title: 'Launch RF Analyst', url: '/static/launch_analysis.html?v=075', description: 'L1/L2/L5 timelines, spectrum and spike analysis.' },
        { title: 'Raw Map Dashboard', url: '/static/index.html?v=0111', description: 'Direct static dashboard view if needed.' }
      ]
    },
    {
      group: 'System',
      items: [
        { title: 'Platform Health', url: '/api/platform/health', description: 'Backend, database and static page health.' },
        { title: 'Deploy Check', url: '/api/platform/deploy-check', description: 'Runtime, static file and database checks.' },
        { title: 'J2 / Rotator Check', url: '/api/platform/j2-rotator-check', description: 'Checks restored J2 and rotator assets.' }
      ]
    }
  ];

  function currentPath() {
    return window.location.pathname.replace(/\/+$/, '') || '/';
  }

  function isActive(url) {
    try {
      const u = new URL(url, window.location.origin);
      const p = u.pathname.replace(/\/+$/, '') || '/';
      const c = currentPath();
      if (p === '/' && (c === '/' || c.endsWith('/index.html'))) return true;
      if (p === '/j2' && (c === '/j2' || c.endsWith('/j2_report.html'))) return true;
      if (p === '/rotator' && (c === '/rotator' || c.endsWith('/rotator.html'))) return true;
      return p === c;
    } catch (_err) {
      return false;
    }
  }

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function normalizeGroups(payload) {
    if (!payload || !Array.isArray(payload.groups)) return DEFAULT_GROUPS;
    return payload.groups.map(g => ({
      group: g.group || 'Menu',
      items: Array.isArray(g.items) ? g.items : []
    })).filter(g => g.items.length);
  }

  function render(groups, health) {
    if (document.getElementById('eei-app-shell')) return;
    const status = (health && health.status) || 'check';
    const statusClass = status === 'ok' ? 'good' : status === 'error' ? 'bad' : 'check';
    const statusText = status === 'ok' ? 'API online' : status === 'error' ? 'API fault' : 'Check status';

    const nav = groups.map(group => {
      const items = group.items.map(item => {
        const active = isActive(item.url) ? ' eei-active' : '';
        const available = item.available === false ? ' eei-unavailable' : '';
        const suffix = item.available === false ? ' — missing' : '';
        return `<a class="eei-shell-link${active}${available}" href="${esc(item.url)}"><span class="eei-shell-link-title">${esc(item.title)}${esc(suffix)}</span><span class="eei-shell-link-desc">${esc(item.description || '')}</span></a>`;
      }).join('');
      return `<div class="eei-shell-group"><button type="button" aria-haspopup="true">${esc(group.group)}</button><div class="eei-shell-menu">${items}</div></div>`;
    }).join('');

    const shell = document.createElement('div');
    shell.id = 'eei-app-shell';
    shell.innerHTML = `
      <div class="eei-shell-inner">
        <div class="eei-shell-brand"><a href="/app?v=0111">EEI LANTERN</a><span>Launch Analysis and Network Telemetry Evaluation for RF Navigation | v${VERSION}</span></div>
        <button class="eei-shell-toggle" type="button" aria-label="Open platform menu">Menu</button>
        <nav class="eei-shell-nav" aria-label="EEI LANTERN platform navigation">${nav}</nav>
        <div class="eei-shell-actions">
          <span class="eei-shell-status"><span class="eei-shell-dot ${statusClass}"></span>${esc(statusText)}</span>
          <a class="eei-shell-action" href="/app?v=0111">Home</a>
        </div>
      </div>`;
    document.body.insertBefore(shell, document.body.firstChild);
    document.body.classList.add('eei-shell-mounted');

    const toggle = shell.querySelector('.eei-shell-toggle');
    toggle && toggle.addEventListener('click', () => shell.classList.toggle('eei-mobile-open'));
    shell.querySelectorAll('.eei-shell-group > button').forEach(btn => {
      btn.addEventListener('click', () => {
        const group = btn.closest('.eei-shell-group');
        shell.querySelectorAll('.eei-shell-group.eei-open').forEach(g => { if (g !== group) g.classList.remove('eei-open'); });
        group && group.classList.toggle('eei-open');
      });
    });
    document.addEventListener('keydown', ev => {
      if (ev.key === 'Escape') {
        shell.classList.remove('eei-mobile-open');
        shell.querySelectorAll('.eei-shell-group.eei-open').forEach(g => g.classList.remove('eei-open'));
      }
    });
  }

  async function getJson(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(String(r.status));
    return r.json();
  }

  function start() {
    Promise.allSettled([
      getJson('/api/platform/navigation'),
      getJson('/api/platform/health')
    ]).then(results => {
      const navPayload = results[0].status === 'fulfilled' ? results[0].value : null;
      const health = results[1].status === 'fulfilled' ? results[1].value : { status: 'check' };
      render(normalizeGroups(navPayload), health);
    }).catch(() => render(DEFAULT_GROUPS, { status: 'check' }));
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
