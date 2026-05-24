import './App.css'

const backend = 'http://127.0.0.1:8000'

type PageLink = {
  title: string
  description: string
  href: string
}

const pages: PageLink[] = [
  {
    title: 'Home Dashboard',
    description: 'Main local dashboard entry page.',
    href: `${backend}/static/home.html`,
  },
  {
    title: 'Briefing',
    description: 'EEI-style briefing and decision support view.',
    href: `${backend}/static/briefing.html?v=080`,
  },
  {
    title: 'Launch RF Analysis',
    description: 'L1/L2/L5, spectrum, spikes, and launch-window ranking.',
    href: `${backend}/static/launch_analysis.html?v=080`,
  },
  {
    title: 'Data Quality',
    description: 'Scan validity, filter quality, and evidence checks.',
    href: `${backend}/static/data_quality.html`,
  },
  {
    title: 'Mission Brief',
    description: 'Operational summary and decision framing.',
    href: `${backend}/static/mission_brief.html`,
  },
  {
    title: 'UAS RF',
    description: 'UAS-specific RF review page.',
    href: `${backend}/static/uas_rf.html`,
  },
  {
    title: 'v080 Decision Workflow',
    description: 'Current decision workflow package page.',
    href: `${backend}/static/moth_v080_decision_workflow.html`,
  },
  {
    title: 'API Docs',
    description: 'Backend FastAPI documentation.',
    href: `${backend}/docs`,
  },
]

export default function App() {
  return (
    <main className="shell">
      <section className="hero">
        <p className="eyebrow">MOTH Local</p>
        <h1>Laptop Development Console</h1>
        <p>
          Central launch point for the local MOTH dashboards, RF analysis pages,
          and v080 decision workflow.
        </p>
      </section>

      <section className="grid" aria-label="MOTH local pages">
        {pages.map((page) => (
          <a
            className="card"
            href={page.href}
            target="_blank"
            rel="noreferrer"
            key={page.title}
          >
            <strong>{page.title}</strong>
            <span>{page.description}</span>
          </a>
        ))}
      </section>
    </main>
  )
}
