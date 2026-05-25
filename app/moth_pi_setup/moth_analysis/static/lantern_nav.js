(function () {
  const pages = [
    { group: 'Start', label: 'Home', href: '/static/dashboard.html' },
    { group: 'Workflow', label: '1 Upload + Map', href: '/static/index.html?v=060' },
    { group: 'Workflow', label: '2 Data Quality', href: '/static/data_quality.html' },
    { group: 'Workflow', label: '3 Launch RF', href: '/static/launch_analysis.html?v=080' },
    { group: 'Workflow', label: '4 JSP 101 Report', href: '/static/jsp101_report.html' },
    { group: 'Workflow', label: '5 Mission Brief', href: '/static/mission_brief.html' },
    { group: 'Reference', label: 'EEI Briefing', href: '/static/briefing.html?v=080' },
    { group: 'Reference', label: 'v080 Workflow', href: '/static/moth_v080_decision_workflow.html' },
    { group: 'Reference', label: 'UAS RF', href: '/static/uas_rf.html' },
    { group: 'System', label: 'API Docs', href: '/docs' }
  ];

  if (document.getElementById('lantern-flow-nav')) return;

  const current = window.location.pathname.toLowerCase();

  const nav = document.createElement('div');
  nav.id = 'lantern-flow-nav';

  const button = document.createElement('button');
  button.id = 'lantern-flow-toggle';
  button.type = 'button';
  button.textContent = 'LANTERN Flow';

  const panel = document.createElement('div');
  panel.id = 'lantern-flow-panel';

  let lastGroup = '';

  pages.forEach((page) => {
    if (page.group !== lastGroup) {
      const heading = document.createElement('div');
      heading.className = 'lantern-flow-heading';
      heading.textContent = page.group;
      panel.appendChild(heading);
      lastGroup = page.group;
    }

    const link = document.createElement('a');
    link.href = page.href;
    link.textContent = page.label;

    const pagePath = page.href.split('?')[0].toLowerCase();
    if (current === pagePath) {
      link.className = 'active';
    }

    panel.appendChild(link);
  });

  button.addEventListener('click', function () {
    nav.classList.toggle('open');
  });

  nav.appendChild(button);
  nav.appendChild(panel);
  document.body.appendChild(nav);
})();
