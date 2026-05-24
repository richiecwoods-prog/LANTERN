(() => {
  'use strict';

  const GNSS = {
    L1: 1575420000,
    L2: 1227600000,
    L3: 1381050000,
    L5: 1176450000
  };
  const BAND_WIDTH_HZ = 20_000_000;
  const SPIKE_THRESHOLD_DBM = -60;
  const LOCAL_OFFSET_HOURS = 3;

  const state = {
    records: [],
    files: [],
    candidates: [],
    candidateResult: null,
    launchResult: null,
    qualityResult: null
  };

  const $ = (id) => document.getElementById(id);

  function showSection(id) {
    document.querySelectorAll('.section').forEach(section => section.classList.toggle('active', section.id === id));
    document.querySelectorAll('.nav-button').forEach(button => button.classList.toggle('active', button.dataset.sectionTarget === id));
  }

  function setMode(mode) {
    document.body.classList.remove('mode-briefing', 'mode-analyst', 'mode-admin');
    document.body.classList.add(`mode-${mode}`);
  }

  function parseCSVLine(line) {
    const out = [];
    let current = '';
    let quoted = false;
    for (let i = 0; i < line.length; i += 1) {
      const char = line[i];
      const next = line[i + 1];
      if (char === '"' && quoted && next === '"') {
        current += '"';
        i += 1;
      } else if (char === '"') {
        quoted = !quoted;
      } else if (char === ',' && !quoted) {
        out.push(current);
        current = '';
      } else {
        current += char;
      }
    }
    out.push(current);
    return out;
  }

  function normaliseKey(key) {
    return String(key || '').trim().toLowerCase().replace(/[\s-]+/g, '_');
  }

  function firstValue(row, keys) {
    for (const key of keys) {
      if (row[key] !== undefined && row[key] !== null && row[key] !== '') return row[key];
    }
    return null;
  }

  function toNumber(value) {
    if (value === undefined || value === null || value === '') return null;
    const parsed = Number(String(value).replace(/,/g, '').trim());
    return Number.isFinite(parsed) ? parsed : null;
  }

  function parseTimestamp(value) {
    if (!value) return null;
    const text = String(value).trim();
    const numeric = Number(text);
    if (Number.isFinite(numeric)) {
      if (numeric > 10_000_000_000) return new Date(numeric);
      if (numeric > 1_000_000_000) return new Date(numeric * 1000);
    }
    let parsed = new Date(text);
    if (!Number.isNaN(parsed.getTime())) return parsed;
    parsed = new Date(text.replace(' ', 'T') + 'Z');
    if (!Number.isNaN(parsed.getTime())) return parsed;
    return null;
  }

  function parseFrequency(row) {
    const value = firstValue(row, ['frequency_hz', 'freq_hz', 'frequency', 'freq', 'center_frequency_hz', 'center_frequency', 'mhz', 'freq_mhz']);
    let freq = toNumber(value);
    if (freq === null) return null;
    if (freq < 100_000) freq *= 1_000_000;
    return freq;
  }

  function rowToRecord(row, sourceFile, rowNumber) {
    const timestamp = parseTimestamp(firstValue(row, ['timestamp', 'time', 'datetime', 'date_time', 'utc', 'created_at', 'logged_at']));
    const lat = toNumber(firstValue(row, ['lat', 'latitude', 'gps_lat', 'y']));
    const lon = toNumber(firstValue(row, ['lon', 'lng', 'longitude', 'gps_lon', 'gps_lng', 'x']));
    return {
      timestamp,
      lat,
      lon,
      frequencyHz: parseFrequency(row),
      dbm: toNumber(firstValue(row, ['dbm', 'signal_dbm', 'strength_dbm', 'rssi', 'power_dbm', 'level_dbm', 'power', 'level'])),
      sourceFile,
      rowNumber
    };
  }

  function parseCSV(text, sourceFile) {
    const lines = text.split(/\r?\n/).filter(line => line.trim() !== '');
    if (lines.length < 2) return [];
    const headers = parseCSVLine(lines[0]).map(normaliseKey);
    const rows = [];
    for (let i = 1; i < lines.length; i += 1) {
      const values = parseCSVLine(lines[i]);
      const row = {};
      headers.forEach((header, idx) => { row[header] = values[idx] || ''; });
      rows.push(rowToRecord(row, sourceFile, i + 1));
    }
    return rows;
  }

  function validGPS(record) {
    return record.lat !== null && record.lon !== null && record.lat >= -90 && record.lat <= 90 && record.lon >= -180 && record.lon <= 180 && !(Math.abs(record.lat) < 1e-9 && Math.abs(record.lon) < 1e-9);
  }

  function bandFor(record) {
    if (record.frequencyHz === null) return null;
    for (const [band, centre] of Object.entries(GNSS)) {
      if (Math.abs(record.frequencyHz - centre) <= BAND_WIDTH_HZ) return band;
    }
    return null;
  }

  function selectedBands() {
    const value = $('bandPreset').value;
    if (value === 'CUSTOM') return ['CUSTOM'];
    return value.split(',').map(item => item.trim());
  }

  function recordInSelectedBands(record) {
    const bands = selectedBands();
    if (bands.includes('CUSTOM')) {
      const minHz = Number($('customMinHz').value);
      const maxHz = Number($('customMaxHz').value);
      return record.frequencyHz !== null && record.frequencyHz >= minHz && record.frequencyHz <= maxHz;
    }
    return bands.includes(bandFor(record));
  }

  function formatTime(date) {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return 'N/A';
    return date.toISOString().slice(11, 16);
  }

  function addMinutes(date, minutes) {
    return new Date(date.getTime() + minutes * 60_000);
  }

  function floorToStep(date, stepMinutes) {
    const copy = new Date(date.getTime());
    copy.setUTCSeconds(0, 0);
    copy.setUTCMinutes(Math.floor(copy.getUTCMinutes() / stepMinutes) * stepMinutes);
    return copy;
  }

  function categoryFromLaunchScore(score, spikes) {
    if (score >= 82 && spikes === 0) return 'RECOMMENDED';
    if (score >= 62) return 'BEST VIABLE';
    if (score >= 42) return 'LEAST-BUSY OBSERVED';
    return 'AVOID IF POSSIBLE';
  }

  function qualityClass(category) {
    const text = String(category || '').toLowerCase();
    if (text.includes('recommended') || text.includes('high') || text.includes('pass')) return 'recommended';
    if (text.includes('best') || text.includes('medium') || text.includes('check') || text.includes('least')) return 'best-viable';
    return 'avoid';
  }

  function statusLabel(status) {
    const cls = status === 'PASS' ? 'status-pass' : status === 'FAIL' ? 'status-fail' : 'status-check';
    return `<span class="status-label ${cls}">${status}</span>`;
  }

  function updateGlobalSummary() {
    const rows = state.records.length;
    const files = state.files.length ? state.files.join(', ') : 'none';
    const bands = selectedBands().join('/');
    const quality = state.qualityResult ? state.qualityResult.category : 'unknown';
    $('globalSummary').innerHTML = `<strong>Current view</strong>Scans: ${files}. Rows: ${rows}. Frequency: ${bands}. Time: full survey. Confidence: ${quality}.`;
    $('filterSummary').innerHTML = `<strong>Current view</strong>Scans: ${files}. Frequency: ${bands}. Time: full survey. Map mode: small suitability hexagons. Confidence: ${quality}.`;
    $('adminImportStatus').textContent = `${state.files.length} file(s), ${rows} parsed row(s).`;
  }

  function calculateQuality() {
    const total = state.records.length;
    if (!total) {
      return {
        category: 'NO DATA',
        score: 0,
        reason: 'No records were available for analysis.',
        recommendation: 'Upload at least one MOTH/LAMP CSV before scoring candidates or launch windows.',
        checks: []
      };
    }
    const validGps = state.records.filter(validGPS).length;
    const zeroRows = state.records.filter(r => r.lat !== null && r.lon !== null && Math.abs(r.lat) < 1e-9 && Math.abs(r.lon) < 1e-9).length;
    const timestamps = state.records.map(r => r.timestamp).filter(d => d instanceof Date && !Number.isNaN(d.getTime())).sort((a, b) => a - b);
    const freqValid = state.records.filter(r => r.frequencyHz !== null && r.frequencyHz >= 5_000_000 && r.frequencyHz <= 6_000_000_000).length;
    const scanCount = new Set(state.records.map(r => r.sourceFile)).size;
    const coveredBands = [...new Set(state.records.map(bandFor).filter(Boolean))].sort();
    const missingBands = ['L1', 'L2', 'L5'].filter(b => !coveredBands.includes(b));
    let spanMinutes = 0;
    const gaps = [];
    if (timestamps.length > 1) {
      spanMinutes = (timestamps[timestamps.length - 1] - timestamps[0]) / 60_000;
      for (let i = 1; i < timestamps.length; i += 1) {
        const gap = (timestamps[i] - timestamps[i - 1]) / 60_000;
        if (gap > 45) gaps.push(gap);
      }
    }
    const seen = new Map();
    let duplicates = 0;
    state.records.forEach(r => {
      const key = `${r.timestamp ? r.timestamp.toISOString() : ''}|${r.lat}|${r.lon}|${r.frequencyHz}|${r.dbm}`;
      if (seen.has(key)) duplicates += 1;
      seen.set(key, true);
    });
    const gpsPct = validGps / total * 100;
    const freqPct = freqValid / total * 100;
    const timestampPct = timestamps.length / total * 100;
    let score = 100;
    if (gpsPct < 95) score -= Math.min(35, (95 - gpsPct) * 0.7);
    if (zeroRows) score -= Math.min(20, zeroRows / total * 100);
    if (timestampPct < 98) score -= Math.min(25, (98 - timestampPct) * 0.6);
    if (freqPct < 98) score -= Math.min(25, (98 - freqPct) * 0.6);
    if (spanMinutes < 30) score -= 25; else if (spanMinutes < 120) score -= 10;
    if (scanCount < 2) score -= 8;
    if (gaps.length) score -= Math.min(15, gaps.length * 3);
    if (duplicates) score -= Math.min(15, duplicates / total * 100);
    if (missingBands.length) score -= Math.min(20, missingBands.length * 7);
    score = Math.max(0, Math.min(100, score));
    const category = score >= 80 ? 'HIGH' : score >= 55 ? 'MEDIUM' : score >= 30 ? 'LOW' : 'NO DATA';
    const reason = `${gpsPct >= 95 ? 'good GPS coverage' : `GPS coverage is only ${gpsPct.toFixed(1)}%`}; ${spanMinutes >= 120 ? 'adequate time span' : 'limited time span'}; ${missingBands.length ? `missing expected band coverage: ${missingBands.join(', ')}` : 'expected GNSS-band coverage present'}.`;
    const recommendation = category === 'HIGH'
      ? 'Proceed to candidate or launch-window validation, then confirm with controlled field checks.'
      : category === 'MEDIUM'
        ? 'Use results cautiously and collect one additional targeted scan before briefing as ready.'
        : 'Do not rely on this dataset for a decision until GPS, timing, duplicates or band coverage are corrected.';
    return {
      category, score: Number(score.toFixed(1)), reason, recommendation,
      checks: [
        { check: 'Valid GPS percentage', value: `${gpsPct.toFixed(1)}%`, status: gpsPct >= 95 ? 'PASS' : 'CHECK', why: 'Bad GPS makes map output unreliable.' },
        { check: '0,0 coordinate rows', value: zeroRows, status: zeroRows === 0 ? 'PASS' : 'FAIL', why: '0,0 rows must be excluded.' },
        { check: 'Time span covered', value: `${spanMinutes.toFixed(1)} min`, status: spanMinutes >= 120 ? 'PASS' : 'CHECK', why: 'Short scans may mislead.' },
        { check: 'Gaps in time', value: gaps.length, status: gaps.length === 0 ? 'PASS' : 'CHECK', why: 'Missing periods hide patterns.' },
        { check: 'Scan count', value: scanCount, status: scanCount >= 2 ? 'PASS' : 'CHECK', why: 'Multiple scans improve confidence.' },
        { check: 'Duplicate rows', value: duplicates, status: duplicates === 0 ? 'PASS' : 'CHECK', why: 'Duplicates distort density.' },
        { check: 'Frequency coverage', value: coveredBands.join(', ') || 'None', status: missingBands.length ? 'CHECK' : 'PASS', why: 'Confirms the relevant bands were scanned.' },
        { check: 'MOTH nominal range rows', value: `${freqPct.toFixed(1)}%`, status: freqPct >= 98 ? 'PASS' : 'CHECK', why: 'Expected record frequencies should fall between 5 MHz and 6 GHz.' }
      ]
    };
  }

  function renderQuality() {
    state.qualityResult = calculateQuality();
    const result = state.qualityResult;
    const card = $('qualityDecision');
    card.className = `card decision-card ${qualityClass(result.category)}`;
    card.innerHTML = `
      <div class="decision-eyebrow">Is the data good enough?</div>
      <h3 class="decision-title">Data quality: ${result.category}</h3>
      <div class="decision-score"><span class="score-pill">${result.score}/100</span><span class="category-pill">${result.category}</span></div>
      <p><strong>Reason:</strong> ${result.reason}</p>
      <p><strong>Recommendation:</strong> ${result.recommendation}</p>
      <button id="explainQuality" class="secondary">Explain this result</button>
      <div id="qualityExplanation" class="explanation"></div>
    `;
    $('qualityTable').querySelector('tbody').innerHTML = result.checks.map(item => `<tr><td>${item.check}</td><td>${item.value}</td><td>${statusLabel(item.status)}</td><td>${item.why}</td></tr>`).join('');
    $('explainQuality').addEventListener('click', () => {
      const box = $('qualityExplanation');
      box.textContent = `Data quality is ${result.category} with score ${result.score}. Reason: ${result.reason} Recommendation: ${result.recommendation}`;
      box.classList.toggle('active');
    });
    updateGlobalSummary();
  }

  function runWithProgress(button, progressBox, steps, action) {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = 'Working...';
    progressBox.classList.add('active');
    const fill = progressBox.querySelector('.progress-fill');
    const text = progressBox.querySelector('.progress-text');
    let index = 0;
    return new Promise(resolve => {
      const tick = () => {
        if (index < steps.length) {
          fill.style.width = `${((index + 1) / steps.length) * 100}%`;
          text.textContent = `Step ${index + 1} of ${steps.length}: ${steps[index]}`;
          index += 1;
          setTimeout(tick, 180);
        } else {
          const result = action();
          setTimeout(() => {
            progressBox.classList.remove('active');
            button.disabled = false;
            button.textContent = original;
            resolve(result);
          }, 140);
        }
      };
      tick();
    });
  }

  function rankLaunchWindows() {
    const records = state.records.filter(r => r.timestamp instanceof Date && !Number.isNaN(r.timestamp.getTime()) && recordInSelectedBands(r));
    if (!records.length) {
      return {
        decision: {
          category: 'NO DATA', score: 0, confidence: 'Low', utcWindow: 'N/A', localWindow: 'N/A',
          reason: 'No matching records were available.', caution: 'Do not make a timing decision from this filter set.',
          nextAction: 'Select scans with L1/L2/L5 coverage or collect a new scan.', metrics: {}
        },
        windows: [], series: []
      };
    }
    const windowMinutes = Number($('windowMinutes').value) || 30;
    const stepMinutes = Number($('stepMinutes').value) || 10;
    const times = records.map(r => r.timestamp).sort((a, b) => a - b);
    let current = floorToStep(times[0], stepMinutes);
    const latest = times[times.length - 1];
    const windows = [];
    const series = [];
    while (current <= latest) {
      const end = addMinutes(current, windowMinutes);
      const inside = records.filter(r => r.timestamp >= current && r.timestamp < end);
      const counts = { L1: 0, L2: 0, L5: 0 };
      inside.forEach(r => { const band = bandFor(r); if (counts[band] !== undefined) counts[band] += 1; });
      const spikes = inside.filter(r => r.dbm !== null && r.dbm >= SPIKE_THRESHOLD_DBM).length;
      const activeBandCount = Object.values(counts).filter(Boolean).length;
      const countPenalty = Math.min(50, inside.length * 1.8);
      const spikePenalty = Math.min(30, spikes * 8);
      const spreadPenalty = Math.max(0, activeBandCount - 1) * 3;
      const score = Math.max(0, 100 - countPenalty - spikePenalty - spreadPenalty);
      const localStart = addMinutes(current, LOCAL_OFFSET_HOURS * 60);
      const localEnd = addMinutes(end, LOCAL_OFFSET_HOURS * 60);
      const category = categoryFromLaunchScore(score, spikes);
      const item = {
        start: current, end,
        utcWindow: `${formatTime(current)}-${formatTime(end)} UTC`,
        localWindow: `${formatTime(localStart)}-${formatTime(localEnd)} local UTC+${LOCAL_OFFSET_HOURS}`,
        category, score: Number(score.toFixed(1)), confidence: state.records.length >= 100 ? 'High' : 'Medium',
        eventCount: inside.length, spikeCount: spikes, countsByBand: counts,
        mostAffectedBand: Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0]
      };
      windows.push(item);
      series.push({ time: current, eventCount: inside.length, countsByBand: counts });
      current = addMinutes(current, stepMinutes);
    }
    windows.sort((a, b) => b.score - a.score || a.spikeCount - b.spikeCount || a.eventCount - b.eventCount || a.start - b.start);
    const best = windows[0];
    return {
      decision: {
        category: best.category,
        score: best.score,
        confidence: best.confidence,
        utcWindow: best.utcWindow,
        localWindow: best.localWindow,
        reason: `Lower GNSS-band event count, ${best.spikeCount} spikes at or above ${SPIKE_THRESHOLD_DBM} dBm, and most affected band ${best.mostAffectedBand}.`,
        caution: best.category === 'RECOMMENDED' ? 'Still requires authorised operational approval and normal UAS safety checks.' : 'Not a clean window; validate before launch.',
        nextAction: 'Review L1/L2/L5 graph, check spikes, and generate launch brief.',
        metrics: best
      },
      windows,
      series
    };
  }

  function renderLaunch() {
    const result = state.launchResult;
    const d = result.decision;
    const card = $('launchDecision');
    card.className = `card decision-card ${qualityClass(d.category)}`;
    card.innerHTML = `
      <div class="decision-eyebrow">Best launch timing</div>
      <h3 class="decision-title">${d.utcWindow} / ${d.localWindow}</h3>
      <div class="decision-score"><span class="score-pill">${d.score}/100</span><span class="category-pill">${d.category}</span><span class="category-pill">Confidence: ${d.confidence}</span></div>
      <p><strong>Reason:</strong> ${d.reason}</p>
      <p><strong>Caution:</strong> ${d.caution}</p>
      <p><strong>Next action:</strong> ${d.nextAction}</p>
      <button id="explainLaunch" class="secondary">Explain this result</button>
      <div id="launchExplanation" class="explanation"></div>
    `;
    $('explainLaunch').addEventListener('click', () => {
      const box = $('launchExplanation');
      const m = d.metrics || {};
      box.textContent = `This window was selected because it had ${m.eventCount ?? 'unknown'} selected-band events, ${m.spikeCount ?? 'unknown'} strong spikes at or above ${SPIKE_THRESHOLD_DBM} dBm, and lower overall activity than surrounding windows. It is classed as ${d.category}.`;
      box.classList.toggle('active');
    });
    $('windowTable').querySelector('tbody').innerHTML = result.windows.slice(0, 12).map(w => `<tr><td>${w.utcWindow}</td><td>${w.localWindow}</td><td>${w.category}</td><td>${w.score}</td><td>${w.eventCount}</td><td>${w.spikeCount}</td></tr>`).join('');
    const quietest = result.windows[0];
    const noisiest = [...result.windows].sort((a, b) => b.eventCount - a.eventCount || b.spikeCount - a.spikeCount)[0];
    $('graphResult').innerHTML = `<strong>Graph result</strong>Quietest period shown: ${quietest ? quietest.utcWindow : 'N/A'}. Noisiest period shown: ${noisiest ? noisiest.utcWindow : 'N/A'}. Most affected band: ${quietest ? quietest.mostAffectedBand : 'N/A'}.`;
    drawLaunchChart(result.series);
  }

  function drawLaunchChart(series) {
    const canvas = $('launchChart');
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = '#d6dde6';
    ctx.lineWidth = 1;
    const left = 54, right = 20, top = 24, bottom = 48;
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, canvas.height - bottom);
    ctx.lineTo(canvas.width - right, canvas.height - bottom);
    ctx.stroke();
    if (!series.length) return;
    const max = Math.max(1, ...series.map(s => s.eventCount));
    const width = (canvas.width - left - right) / series.length;
    ctx.fillStyle = '#0b578b';
    series.forEach((s, i) => {
      const h = (s.eventCount / max) * (canvas.height - top - bottom - 10);
      ctx.fillRect(left + i * width + 2, canvas.height - bottom - h, Math.max(2, width - 4), h);
    });
    ctx.fillStyle = '#617184';
    ctx.font = '13px system-ui, sans-serif';
    ctx.fillText('MOTH event count per scored bucket', left, 16);
    ctx.fillText('Time UTC', canvas.width / 2 - 28, canvas.height - 12);
    ctx.fillText(String(max), 10, top + 5);
    ctx.fillText('0', 18, canvas.height - bottom + 4);
    const first = series[0].time;
    const last = series[series.length - 1].time;
    ctx.fillText(formatTime(first), left, canvas.height - 28);
    ctx.fillText(formatTime(last), canvas.width - right - 46, canvas.height - 28);
  }

  function haversineMeters(lat1, lon1, lat2, lon2) {
    const R = 6371000;
    const toRad = deg => deg * Math.PI / 180;
    const dLat = toRad(lat2 - lat1);
    const dLon = toRad(lon2 - lon1);
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function percentile(values, pct) {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    const k = (sorted.length - 1) * pct / 100;
    const f = Math.floor(k), c = Math.ceil(k);
    if (f === c) return sorted[f];
    return sorted[f] * (c - k) + sorted[c] * (k - f);
  }

  function normaliseDbm(dbm, low = -105, high = -50) {
    if (dbm === null || dbm === undefined) return 0;
    return Math.max(0, Math.min(1, (dbm - low) / (high - low)));
  }

  function scoreCandidates() {
    if (!state.candidates.length) {
      state.candidates.push({ name: 'Candidate A', lat: 2.0462, lon: 45.3182 });
      state.candidates.push({ name: 'Candidate B', lat: 2.0468, lon: 45.3188 });
    }
    const valid = state.records.filter(r => validGPS(r) && r.frequencyHz !== null);
    const scored = state.candidates.map(candidate => {
      const nearby = valid.filter(r => haversineMeters(candidate.lat, candidate.lon, r.lat, r.lon) <= 100);
      const target = nearby.filter(r => ['L1', 'L2', 'L5'].includes(bandFor(r)));
      const nonTargetStrong = nearby.filter(r => !['L1', 'L2', 'L5'].includes(bandFor(r)) && r.dbm !== null && r.dbm >= -65);
      const dbms = target.map(r => r.dbm).filter(v => v !== null);
      const lowerTail = percentile(dbms, 10);
      const median = percentile(dbms, 50);
      const eventScore = Math.min(20, Math.log1p(target.length) * 5);
      const score = Math.max(0, Math.min(100, eventScore + normaliseDbm(lowerTail) * 35 + normaliseDbm(median) * 15 + Math.min(20, nearby.length / 5) - Math.min(30, nonTargetStrong.length * 4) + 10));
      const confidence = target.length >= 25 && nearby.length >= 30 ? 'High' : target.length >= 8 && nearby.length >= 10 ? 'Medium' : 'Low';
      return { ...candidate, score: Number(score.toFixed(1)), confidence, targetDetections: target.length, nearbyDetections: nearby.length, lowerTailDbm: lowerTail === null ? null : Number(lowerTail.toFixed(1)), medianDbm: median === null ? null : Number(median.toFixed(1)), strongNonTargetEvents: nonTargetStrong.length, timelineStatus: confidence !== 'Low' && nonTargetStrong.length < 5 ? 'GOOD' : target.length ? 'CHECK' : 'UNDEFINED' };
    }).sort((a, b) => b.score - a.score || a.strongNonTargetEvents - b.strongNonTargetEvents || b.targetDetections - a.targetDetections);
    scored.forEach((item, idx) => { item.rank = idx + 1; });
    const best = scored[0];
    return {
      decision: {
        candidate: best.name, score: best.score, confidence: best.confidence,
        reason: `Ranked highest with ${best.targetDetections} target-band detections, lower-tail strength ${best.lowerTailDbm} dBm, median ${best.medianDbm} dBm, and ${best.strongNonTargetEvents} strong non-target events.`,
        nextAction: 'Repeat controlled survey at this point and compare with the next two candidates.', metrics: best
      },
      candidates: scored
    };
  }

  function renderCandidate() {
    const result = state.candidateResult;
    const d = result.decision;
    const card = $('candidateDecision');
    card.className = `card decision-card ${qualityClass(d.confidence)}`;
    card.innerHTML = `
      <div class="decision-eyebrow">Recommended antenna candidate</div>
      <h3 class="decision-title">${d.candidate}</h3>
      <div class="decision-score"><span class="score-pill">${d.score}/100</span><span class="category-pill">Confidence: ${d.confidence}</span></div>
      <p><strong>Reason:</strong> ${d.reason}</p>
      <p><strong>Next action:</strong> ${d.nextAction}</p>
      <button id="explainCandidate" class="secondary">Explain this result</button>
      <div id="candidateExplanation" class="explanation"></div>
    `;
    $('explainCandidate').addEventListener('click', () => {
      const box = $('candidateExplanation');
      const m = d.metrics || {};
      box.textContent = `This candidate ranked highest because it had ${m.targetDetections} target-band detections, lower-tail strength ${m.lowerTailDbm} dBm, median strength ${m.medianDbm} dBm, and ${m.strongNonTargetEvents} strong non-target events. The score is deterministic; this text only explains it.`;
      box.classList.toggle('active');
    });
    $('candidateTable').querySelector('tbody').innerHTML = result.candidates.map(c => `<tr><td>${c.name}</td><td>${c.lat.toFixed(6)}</td><td>${c.lon.toFixed(6)}</td><td>${c.score}</td><td>${c.confidence}</td></tr>`).join('');
  }

  function renderCandidateTableOnly() {
    $('candidateTable').querySelector('tbody').innerHTML = state.candidates.map(c => `<tr><td>${c.name}</td><td>${Number(c.lat).toFixed(6)}</td><td>${Number(c.lon).toFixed(6)}</td><td>-</td><td>-</td></tr>`).join('');
  }

  function generateCandidateReport() {
    if (!state.candidateResult) state.candidateResult = scoreCandidates();
    const d = state.candidateResult.decision;
    const m = d.metrics || {};
    $('candidateReport').value = `# Candidate Site Report\n\nCandidate name: ${d.candidate}\nScore: ${d.score}/100\nConfidence: ${d.confidence}\n\n## Why selected / rejected\n${d.reason}\n\n## Evidence summary\n- Target detections: ${m.targetDetections ?? 'N/A'}\n- Lower-tail dBm: ${m.lowerTailDbm ?? 'N/A'}\n- Median dBm: ${m.medianDbm ?? 'N/A'}\n- Strong non-target events: ${m.strongNonTargetEvents ?? 'N/A'}\n- Timeline status: ${m.timelineStatus ?? 'N/A'}\n\n## Recommended next action\n${d.nextAction}\n\nRF planning aid only. Final launch decision requires authorised operational approval and normal UAS safety checks.`;
  }

  function generateLaunchReport() {
    if (!state.launchResult) state.launchResult = rankLaunchWindows();
    const d = state.launchResult.decision;
    const m = d.metrics || {};
    const counts = m.countsByBand ? Object.entries(m.countsByBand).map(([band, count]) => `${band}: ${count}`).join(', ') : 'N/A';
    $('launchReport').value = `# Launch Window Report\n\nRecommended timing: ${d.utcWindow}\nLocal time conversion: ${d.localWindow}\nRF score: ${d.score}/100\nCategory: ${d.category}\nConfidence: ${d.confidence}\n\n## L1/L2/L5 status\n${counts}\n\n## Spike status\nSpikes at or above threshold: ${m.spikeCount ?? 'N/A'}\nSpike threshold dBm: ${SPIKE_THRESHOLD_DBM}\n\n## Pattern and caution\n${d.reason}\nCaution: ${d.caution}\n\n## Operator checklist\n- Confirm scans selected are current and representative.\n- Review graph and spike list.\n- Confirm UAS airspace, weather, aircraft health, C2 link, GNSS receiver and crew readiness checks.\n- Record final approval authority.\n\nRF planning aid only. Final launch decision requires authorised operational approval and normal UAS safety checks.`;
  }

  function loadDemoRecords() {
    const base = new Date('2026-05-12T06:00:00Z');
    const rows = [];
    for (let i = 0; i < 36; i += 1) {
      const t = addMinutes(base, 10 * i);
      for (const [band, centre] of [['L1', GNSS.L1], ['L2', GNSS.L2], ['L5', GNSS.L5]]) {
        const count = (i >= 8 && i <= 14 && band !== 'L2') ? 1 : (i >= 20 && i <= 25) ? 3 : 0;
        for (let e = 0; e < count; e += 1) {
          rows.push({ timestamp: t, lat: 2.046 + (i % 5) * 0.0002, lon: 45.318 + (e % 4) * 0.0002, frequencyHz: centre + (e - 1) * 500_000, dbm: i < 20 ? -82 + e * 4 : -58 + e, sourceFile: 'demo.csv', rowNumber: rows.length + 1 });
        }
      }
    }
    state.records = rows;
    state.files = ['demo.csv'];
    state.qualityResult = null;
    state.launchResult = null;
    state.candidateResult = null;
    updateGlobalSummary();
  }

  function bindEvents() {
    document.querySelectorAll('[data-section-target]').forEach(el => el.addEventListener('click', () => showSection(el.dataset.sectionTarget)));
    document.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener('change', () => setMode(input.value)));
    $('bandPreset').addEventListener('change', () => {
      $('customFreqFields').classList.toggle('hidden', $('bandPreset').value !== 'CUSTOM');
      updateGlobalSummary();
    });
    $('csvFiles').addEventListener('change', async (event) => {
      const files = [...event.target.files];
      const loaded = [];
      for (const file of files) {
        const text = await file.text();
        loaded.push(...parseCSV(text, file.name));
      }
      state.records = loaded;
      state.files = files.map(f => f.name);
      state.qualityResult = null;
      state.launchResult = null;
      state.candidateResult = null;
      updateGlobalSummary();
    });
    $('loadDemo').addEventListener('click', loadDemoRecords);
    $('clearData').addEventListener('click', () => {
      state.records = [];
      state.files = [];
      state.qualityResult = null;
      state.launchResult = null;
      state.candidateResult = null;
      updateGlobalSummary();
    });
    $('runQuality').addEventListener('click', renderQuality);
    $('runLaunch').addEventListener('click', () => runWithProgress($('runLaunch'), $('launchProgress'), ['filtering data', 'calculating launch windows', 'checking spikes', 'building summary'], () => {
      state.launchResult = rankLaunchWindows();
      renderLaunch();
      return state.launchResult;
    }));
    $('addCandidate').addEventListener('click', () => {
      state.candidates.push({ name: $('candidateName').value || `Candidate ${state.candidates.length + 1}`, lat: Number($('candidateLat').value), lon: Number($('candidateLon').value) });
      renderCandidateTableOnly();
    });
    $('scoreCandidates').addEventListener('click', () => runWithProgress($('scoreCandidates'), $('candidateProgress'), ['filtering data', 'calculating candidate evidence', 'building summary'], () => {
      state.candidateResult = scoreCandidates();
      renderCandidate();
      return state.candidateResult;
    }));
    $('generateCandidateReport').addEventListener('click', generateCandidateReport);
    $('generateLaunchReport').addEventListener('click', generateLaunchReport);
  }

  bindEvents();
  updateGlobalSummary();
})();
