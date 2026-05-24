import './App.css'

const backend = 'http://127.0.0.1:8000'

function App() {
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

      <section className="grid">
        <a href={`${backend}/static/home.html`} target="_blank">
          <strong>Home Dashboard</strong>
          <span>Main local dashboard entry page.</span>
        </a>

        <a href={`${backend}/static/briefing.html?v=080`} target="_blank">
          <strong>Briefing</strong>
          <span>EEI-style briefing and decision support view.</span>
        </a>

        <a href={`${backend}/static/launch_analysis.html?v=080`} target="_blank">
          <strong>Launch RF Analysis</strong>
          <span>L1/L2/L5, spectrum, spikes, and launch-window ranking.</span>
        </a>

        <a href={`${backend}/static/data_quality.html`} target="_blank">
          <strong>Data Quality</strong>
          <span>Scan validity, filter quality, and evidence checks.</span>
        </a>

        <a href={`${backend}/static/mission_brief.html`} target="_blank">
          <strong>Mission Brief</strong>
          <span>Operational summary and decision framing.</span>
        </a>

        <a href={`${backend}/static/uas_rf.html`} target="_blank">
          <strong>UAS RF</strong>
          <span>UAS-specific RF review page.</span>
        </a>

        <a href={`${backend}/static/moth_v080_decision_workflow.html`} target="_blank">
          <strong>v080 Decision Workflow</strong>
          <span>Current decision workflow package page.</span>
        </a>

        <a href={`${backend}/docs`} target="_blank">
          <strong>API Docs</strong>
          <span>Backend FastAPI documentation.</span>
        </a>
      </section>
    </main>
  )
}

export default App