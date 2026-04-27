// Shared data pipeline for breathing visualizations.
// Provides a single bridge to the Polar WebSocket and signal derivations:
// R-peak detection on ECG, orientation-agnostic signed breath signal,
// stillness, battery, device info.

export function createBridge({ url = 'ws://localhost:8765' } = {}) {
  const state = {
    connected: false,
    hr: 60,
    hrInst: 0,
    lastRPeakT: -1,
    breath01: 0.5,     // 0 = exhaled, 1 = inhaled (approximate)
    breathRaw: 0,      // signed dominant-axis residual
    breathEnergy: 0,   // unsigned magnitude (fallback)
    flip: false,
    stillness: 1,
    battery: null,
    device: null,
    onRPeak: null,     // fn(t, intensity, rrMs)
    t0: performance.now() / 1000,
    now() { return performance.now() / 1000 - this.t0; },
    toggleFlip() { this.flip = !this.flip; },
  };

  // ---- R-peak detector (Pan-Tompkins-lite) -----------------------------
  const MW = 15, REFRACTORY = 33; // 115 ms window, 250 ms min RR
  const mwBuf = new Float32Array(MW);
  let mwIdx = 0, mwSum = 0;
  let prevEcg = 0, runningMax = 0.05;
  let sampleTick = 0, lastPeakTick = -9999;
  let rpArmed = true;

  function onEcgSample(v) {
    sampleTick++;
    const d = v - prevEcg;
    prevEcg = v;
    const sq = d * d;
    mwSum -= mwBuf[mwIdx];
    mwBuf[mwIdx] = sq;
    mwSum += sq;
    mwIdx = (mwIdx + 1) % MW;
    const integrated = mwSum / MW;
    runningMax = Math.max(runningMax * 0.9995, integrated);
    const threshold = runningMax * 0.35;
    if (rpArmed && integrated > threshold && (sampleTick - lastPeakTick) > REFRACTORY) {
      lastPeakTick = sampleTick;
      rpArmed = false;
      const now = state.now();
      let rrMs = 0;
      if (state.lastRPeakT > 0) {
        rrMs = (now - state.lastRPeakT) * 1000;
        state.hrInst = Math.round(60000 / rrMs);
      }
      state.lastRPeakT = now;
      const intensity = Math.min(2.2, 0.7 + integrated / runningMax);
      if (state.onRPeak) state.onRPeak(now, intensity, rrMs);
    }
    if (integrated < threshold * 0.4) rpArmed = true;
  }

  // ---- Signed breath extraction -----------------------------------------
  // Track gravity per axis; extract residuals; DC-remove per axis; pick
  // axis with largest instantaneous variance as the "breath axis"; project
  // signed residual onto it; normalize to 0..1 via rolling min/max.
  let gX = 0, gY = 0, gZ = 0;
  let rxB = 0, ryB = 0, rzB = 0;
  let vX = 0, vY = 0, vZ = 0;
  let bMin = -1, bMax = 1;
  let accInit = false;

  // stillness
  let accMagEMA = 0;
  const STILL = 250; // 5 s @ 50 Hz
  const stillBuf = new Float32Array(STILL);
  let stillIdx = 0, stillSum = 0;

  function feedAcc(x, y, z) {
    if (!accInit) {
      gX = x; gY = y; gZ = z;
      accMagEMA = Math.sqrt(x*x + y*y + z*z);
      accInit = true;
    } else {
      gX = gX * 0.99 + x * 0.01;
      gY = gY * 0.99 + y * 0.01;
      gZ = gZ * 0.99 + z * 0.01;
    }
    const rx = x - gX, ry = y - gY, rz = z - gZ;
    rxB = rxB * 0.995 + rx * 0.005;
    ryB = ryB * 0.995 + ry * 0.005;
    rzB = rzB * 0.995 + rz * 0.005;
    const dx = rx - rxB, dy = ry - ryB, dz = rz - rzB;

    vX = vX * 0.99 + dx * dx * 0.01;
    vY = vY * 0.99 + dy * dy * 0.01;
    vZ = vZ * 0.99 + dz * dz * 0.01;

    const maxV = Math.max(vX, vY, vZ);
    const signal = maxV === vX ? dx : maxV === vY ? dy : dz;

    // rolling range tracking — asymmetric decay so transient peaks expand the range
    bMax = signal > bMax ? signal : bMax * 0.9995 + signal * 0.0005;
    bMin = signal < bMin ? signal : bMin * 0.9995 + signal * 0.0005;
    const range = Math.max(1e-3, bMax - bMin);
    let b01 = (signal - bMin) / range;
    if (state.flip) b01 = 1 - b01;
    state.breathRaw = signal;
    state.breath01 = Math.max(0, Math.min(1, b01));

    state.breathEnergy = Math.sqrt(dx*dx + dy*dy + dz*dz);

    // stillness
    const mag = Math.sqrt(x*x + y*y + z*z);
    accMagEMA = accMagEMA * 0.999 + mag * 0.001;
    const dev = Math.abs(mag - accMagEMA) / (accMagEMA + 1e-6);
    stillSum -= stillBuf[stillIdx];
    stillBuf[stillIdx] = dev;
    stillSum += dev;
    stillIdx = (stillIdx + 1) % STILL;
    state.stillness = 1 - Math.min(1, (stillSum / STILL) * 25);
  }

  // ---- WebSocket --------------------------------------------------------
  let ws;
  function connect() {
    ws = new WebSocket(url);
    ws.onopen = () => { state.connected = true; };
    ws.onclose = () => {
      state.connected = false;
      setTimeout(connect, 1000);
    };
    ws.onerror = () => {};
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === 'ecg') {
        for (const s of m.samples) onEcgSample(s / 1000);
      } else if (m.type === 'hr') {
        state.hr = m.bpm;
      } else if (m.type === 'acc') {
        for (const [x, y, z] of m.samples) feedAcc(x, y, z);
      } else if (m.type === 'battery') {
        state.battery = m.pct;
      } else if (m.type === 'device') {
        state.device = m;
      }
    };
  }
  connect();

  return state;
}

// ---- Breathing exercise presets ------------------------------------------

export const exercises = {
  'coherence 5-5': [{ p: 'inhale', d: 5 }, { p: 'exhale', d: 5 }],
  '5 in · 6 out':  [{ p: 'inhale', d: 5 }, { p: 'exhale', d: 6 }],
  'box 4-4-4-4':   [{ p: 'inhale', d: 4 }, { p: 'hold', d: 4 }, { p: 'exhale', d: 4 }, { p: 'hold', d: 4 }],
  'box 6-6-6-6':   [{ p: 'inhale', d: 6 }, { p: 'hold', d: 6 }, { p: 'exhale', d: 6 }, { p: 'hold', d: 6 }],
  '4-7-8 relax':   [{ p: 'inhale', d: 4 }, { p: 'hold', d: 7 }, { p: 'exhale', d: 8 }],
};
export const exerciseNames = Object.keys(exercises);

export function createScheduler(segments) {
  let segIdx = 0, segElapsed = 0, cycles = 0;
  return {
    tick(dt) {
      segElapsed += dt;
      while (segElapsed >= segments[segIdx].d) {
        segElapsed -= segments[segIdx].d;
        segIdx = (segIdx + 1) % segments.length;
        if (segIdx === 0) cycles++;
      }
    },
    current() {
      const seg = segments[segIdx];
      return {
        phase: seg.p, duration: seg.d, elapsed: segElapsed,
        remaining: seg.d - segElapsed, segIdx, cycles, segments,
      };
    },
    targetAmplitude() {
      const seg = segments[segIdx];
      const t = segElapsed / seg.d;
      if (seg.p === 'inhale') return t;
      if (seg.p === 'exhale') return 1 - t;
      const prev = segments[(segIdx - 1 + segments.length) % segments.length];
      return prev.p === 'inhale' ? 1 : 0;
    },
    reset() { segIdx = 0; segElapsed = 0; cycles = 0; },
  };
}

// ---- tiny shared HTML helpers --------------------------------------------

export function navHTML(current) {
  const items = [
    ['index.html', 'ribbon'],
    ['pacer.html', 'pacer'],
    ['wave-match.html', 'wave'],
    ['coherence.html', 'coherence'],
  ];
  return items
    .map(([h, n]) => n === current
      ? `<span style="opacity:0.9;color:#fff;margin-left:12px">${n}</span>`
      : `<a href="${h}" style="color:inherit;text-decoration:none;margin-left:12px">${n}</a>`)
    .join('');
}
