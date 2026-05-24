const map = L.map('map').setView([center.lat, center.lon], center.zoom);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

let layer = L.layerGroup().addTo(map);
let candidateLayer = L.layerGroup().addTo(map);

function colourFor(v) {
  if (v >= 0.75) return '#00ff99';
  if (v >= 0.45) return '#ffd166';
  return '#ff5c5c';
}

async function uploadFile(file) {
  document.getElementById('status').textContent = 'PROCESSING';
  const form = new FormData();
  form.append('file', file);
  const res = await fetch('/upload', { method:'POST', body:form });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'Upload failed');
  render(data);
  document.getElementById('status').textContent = `LOADED ${data.count} DETECTIONS`;
}

function render(data) {
  layer.clearLayers();
  candidateLayer.clearLayers();
  const bounds = [];

  data.detections.forEach(d => {
    const c = colourFor(d.quality_norm || 0);
    const marker = L.circleMarker([d.lat, d.lon], { radius:5, color:c, fillColor:c, fillOpacity:0.7 });
    marker.bindPopup(`RSSI: ${d.rssi ?? 'n/a'}<br>Freq: ${d.freq ?? 'n/a'}<br>Cluster: ${d.cluster}`);
    marker.addTo(layer);
    bounds.push([d.lat, d.lon]);
  });

  const list = document.getElementById('candidates');
  list.innerHTML = '';
  document.getElementById('summary').textContent = `${data.filename}: ${data.count} georeferenced detections. ${data.candidates.length} candidate clusters.`;

  data.candidates.forEach(c => {
    const m = L.marker([c.lat, c.lon]).addTo(candidateLayer);
    m.bindPopup(`<b>${c.recommendation}</b><br>Score: ${c.score}<br>Detections: ${c.detections}<br>${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}`);
    const li = document.createElement('li');
    li.innerHTML = `<b>${c.recommendation}</b> <span class="badge">${c.score}</span><br>${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}<br>Detections: ${c.detections}<br>Mean RSSI: ${c.mean_rssi?.toFixed?.(1) ?? 'n/a'}`;
    list.appendChild(li);
    bounds.push([c.lat, c.lon]);
  });

  if (bounds.length) map.fitBounds(bounds, { padding:[30,30] });
}

document.getElementById('file').addEventListener('change', async e => {
  try { await uploadFile(e.target.files[0]); }
  catch (err) { document.getElementById('status').textContent = 'ERROR'; alert(err.message); }
});
