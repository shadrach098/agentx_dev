// The agentx orb: a sphere drawn as a matrix of dots, in the brand's lime, that shows what agentx is doing.
//   thinking  a calm sphere turning slowly; sparks of light run along a few great circles, each leaving a short fading
//             trail, and only the dots they pass brighten and lift a little
//   working   a few rings of dots lift out and turn on their own axes, in opposite directions, like a mechanism (one
//             agent running a step)
//   team      a team job: the sphere splits into a constellation, one small orb per helper step that is running or
//             finished, circling a small hub; a running one turns its own rings, a finished one glows still. Six at
//             most: with more, the five that matter most (running first) and a cluster for the rest, marked "+N".
//             When the job ends they gather back into one sphere. Under 48 px, where a constellation would be mush,
//             the sphere turns with three bright dots circling it on tilted paths instead
//   success   the shell breathes slowly in and out around a softly shimmering core of light, faint bands drifting up
//             it (a turn just finished)
//   idle      a slow turn, glow dimmed
//   stuck     the dots fold into a cube that turns slowly and breathes, a band of light sweeping down it and its
//             edges lit: agentx is waiting for you
// Every look fades in and out of the others. Drawn on a <canvas> with plain 2D maths (no library), one shared
// animation loop for every orb on the page; the per-dot work writes into buffers made once (no per-dot
// allocations). A large orb can be grabbed with the mouse (or a finger) and spun; let go and it keeps the flick's
// momentum, then settles into its slow turn in the direction it was thrown.
// create(canvas, stateOf, {teamOf}): teamOf, if given, returns {running, finished}, the helper steps of the team job.
(() => {
  "use strict";
  const TAU = Math.PI * 2;
  const reduced = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : {matches: false};
  const SMALL = 48;                                           // under this many px: no constellation

  // Rings of dots, like lines of latitude: each ring is a row in the matrix.
  function sphereDots(rows) {
    const dots = [];
    for (let i = 0; i < rows; i++) {
      const lat = -Math.PI / 2 + ((i + 0.5) * Math.PI) / rows;
      const r = Math.cos(lat), y = Math.sin(lat);
      const n = Math.max(5, Math.round(rows * 2 * r));
      for (let j = 0; j < n; j++) {
        const lon = (j / n) * TAU + ((i % 2) * Math.PI) / n;
        dots.push({sx: r * Math.cos(lon), sy: y, sz: r * Math.sin(lon), ring: i, lat});
      }
    }
    foldIntoCube(dots, rows >= 14 ? 7 : rows >= 10 ? 5 : 4);
    synapses(dots, rows);
    constellation(dots, rows);
    return dots;
  }

  // The cube the dots fold into: an even grid on each face (n dots along an edge). Every grid point gets the dot
  // whose direction is closest, so the faces are full and crisp; the dots left over share a point with another.
  function foldIntoCube(dots, n) {
    const grid = [], size = 0.62;
    for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) for (let k = 0; k < n; k++) {
      if (i && j && k && i < n - 1 && j < n - 1 && k < n - 1) continue;   // the surface only
      const g = [i, j, k].map((v) => ((v / (n - 1)) * 2 - 1) * size);
      const len = Math.hypot(...g);
      grid.push({p: g, dir: g.map((v) => v / len)});
    }
    const near = (d, q) => d.sx * q.dir[0] + d.sy * q.dir[1] + d.sz * q.dir[2];
    const free = new Set(dots);
    for (const q of grid) {                                   // each grid point takes its closest free dot
      let best = null, score = -2;
      for (const d of free) { const v = near(d, q); if (v > score) { score = v; best = d; } }
      if (!best) break;
      free.delete(best);
      [best.cx, best.cy, best.cz] = q.p;
    }
    for (const d of free) {                                   // the rest go to their closest grid point
      let best = grid[0], score = -2;
      for (const q of grid) { const v = near(d, q); if (v > score) { score = v; best = q; } }
      [d.cx, d.cy, d.cz] = best.p;
    }
    // a dot on two faces at once sits on an edge of the cube: drawn brighter, so the shape stays crisp as it turns
    for (const d of dots) d.edge = [d.cx, d.cy, d.cz].filter((v) => Math.abs(v) > size * 0.98).length >= 2;
  }

  const unit = (v) => { const n = Math.hypot(...v); return v.map((c) => c / n); };
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  // a circle around an axis: two unit vectors across it, so a point on it is u cos a + w sin a
  function across(axis) {
    const u = unit(cross(axis, Math.abs(axis[1]) < 0.9 ? [0, 1, 0] : [1, 0, 0]));
    return [u, cross(axis, u)];
  }

  // thinking: the great circles the sparks run along, each at its own pace
  const SPARKS = Array.from({length: 5}, (_, i) => {
    const a = i * 2.4, axis = unit([Math.cos(a) * 0.6, Math.sin(i * 1.7), Math.sin(a) * 0.6]);
    const [u, w] = across(axis);
    return {axis, u, w, speed: 1.3 + (i % 3) * 0.45, phase: i * 1.3};
  });
  const TRAIL = 1.6;                                          // how far behind its spark a trail fades out (radians)
  // each dot near a spark's circle remembers which, where along it, and how near (flat: path, angle, nearness)
  function synapses(dots, rows) {
    const band = rows >= 14 ? 0.16 : 0.24;                    // fewer dots: a wider band, so a trail still shows
    for (const d of dots) {
      d.syn = [];
      SPARKS.forEach((p, n) => {
        const off = Math.abs(d.sx * p.axis[0] + d.sy * p.axis[1] + d.sz * p.axis[2]);   // 0 on the circle
        if (off > band) return;
        const along = Math.atan2(d.sx * p.w[0] + d.sy * p.w[1] + d.sz * p.w[2], d.sx * p.u[0] + d.sy * p.u[1] + d.sz * p.u[2]);
        d.syn.push(n, along, 1 - (off / band) ** 2);
      });
    }
  }

  // team: every seventh dot goes to one of six small orbs or the hub (cluster 6), keeping its place on the sphere,
  // so the sphere splits into shrunken copies of itself that slide apart, and gathers back the same way. The
  // sixth small orb, the one that stands for the rest, can pile its dots into three smaller balls
  const PILE = [[-0.42, -0.24, 0.1], [0.42, -0.2, -0.1], [0, 0.4, 0]];
  const SLOT_AXES = Array.from({length: 6}, (_, k) => unit([Math.sin(k), 0.4, Math.cos(k)]));
  function constellation(dots, rows) {
    dots.forEach((d, i) => {
      d.k = i % 7;
      d.turn = Math.floor((d.ring * 8) / rows) % 3 === 1;    // three bands of it turn while it runs
      const [ox, oy, oz] = PILE[Math.floor(i / 7) % 3];
      d.qx = ox + d.sx * 0.55; d.qy = oy + d.sy * 0.55; d.qz = oz + d.sz * 0.55;
    });
  }
  // small orbs: three bright dots (and short trails) circle the sphere on tilted paths
  const ORBITS = [[[0.2, 1, 0.1], 1.2, 0], [[1, 0.3, 0.2], -0.9, 2.1], [[0.3, -0.4, 1], 1.5, 4.2]].map(([a, speed, phase]) => {
    const axis = unit(a);
    return {axis, u: across(axis)[0], speed, phase};
  });
  const TAIL = 5;                                             // the head and four trailing dots

  // the rings that turn while it works: each on its own axis, alternating direction
  function workingRings(rows) {
    const picks = rows >= 14 ? [3, Math.floor(rows / 2), rows - 4] : [2, Math.floor(rows / 2), rows - 3];
    const axes = [[1, 0, 0], [0.5, 0, 0.87], [0, 0.4, 0.92]];
    return picks.map((ring, n) => ({ring, axis: axes[n], speed: (n % 2 ? -1 : 1) * (1.1 + n * 0.35)}));
  }

  // the rotations write into P, so turning a dot allocates nothing
  const P = new Float64Array(3);
  function turnAxis(x, y, z, axis, c, s) {                   // Rodrigues' rotation about a unit axis
    const ax = axis[0], ay = axis[1], az = axis[2], d = (ax * x + ay * y + az * z) * (1 - c);
    P[0] = x * c + (ay * z - az * y) * s + ax * d;
    P[1] = y * c + (az * x - ax * z) * s + ay * d;
    P[2] = z * c + (ax * y - ay * x) * s + az * d;
  }
  function view(x, y, z, cy, sy, cx, sx) {                   // turn about y, then lean about x
    const x1 = x * cy + z * sy, z1 = -x * sy + z * cy;
    P[0] = x1; P[1] = y * cx - z1 * sx; P[2] = y * sx + z1 * cx;
  }
  // scratch for one frame: per working ring, per spark, per cluster of the constellation
  const RING_C = new Float64Array(3), RING_S = new Float64Array(3), HEADS = new Float64Array(SPARKS.length);
  const CX = new Float64Array(7), CY = new Float64Array(7), CZ = new Float64Array(7), CS = new Float64Array(7);
  const SPIN_C = new Float64Array(7), SPIN_S = new Float64Array(7), TURN_C = new Float64Array(6), TURN_S = new Float64Array(6);

  function brandColor(canvas) {
    const raw = getComputedStyle(canvas).getPropertyValue("--brand").trim() || "#b8ff3e";
    const probe = document.createElement("span");
    probe.style.color = raw;
    document.body.append(probe);
    const m = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g) || [184, 255, 62];
    probe.remove();
    return m.slice(0, 3).map(Number);
  }

  const orbs = new Set();
  let frame = 0, last = 0;

  function create(canvas, stateOf, {rows, manual, interactive, teamOf} = {}) {
    const size = Number(canvas.dataset.size) || canvas.clientWidth || 32;
    rows = rows || (size >= 64 ? 18 : size >= 28 ? 11 : 8);
    const small = size < SMALL, dots = sphereDots(rows), rings = workingRings(rows);
    for (const d of dots) d.wr = rings.findIndex((r) => r.ring === d.ring);
    const n = dots.length + (small ? ORBITS.length * TAIL : 0);
    const orb = {
      canvas, size, rows, stateOf, teamOf, small, dots, rings,
      t: 0, spin: 0.6, ringAngle: 0, think: 0, work: 0, team: 0, done: 0, cube: 0, glow: 0.4, color: null,
      tilt: REST_TILT, vel: 0, tiltVel: 0, dir: 1, held: false,
      // the constellation's layout, eased: per small orb, how present, how much it runs, where on the circle, its turns
      shown: 1, rest: 0, restN: 0, label: 0, orbit: 0.86, scale: 0.3, hubSpin: 0,
      pres: new Float64Array(6), run: new Float64Array(6), runTo: new Float64Array(6), ang: new Float64Array(6),
      own: new Float64Array(6), turn: new Float64Array(6),
      // what each dot looks like this frame: where, how deep it reads, lit, how strong, how big; drawn far to near
      X: new Float32Array(n), Y: new Float32Array(n), Z: new Float32Array(n), S: new Float32Array(n),
      L: new Float32Array(n), A: new Float32Array(n), F: new Float32Array(n), order: new Uint16Array(n).map((_, i) => i),
    };
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const pad = size * 0.5;                                   // room for the glow around the dots
    canvas.width = Math.round((size + pad * 2) * dpr);
    canvas.height = Math.round((size + pad * 2) * dpr);
    canvas.style.width = canvas.style.height = `${size + pad * 2}px`;
    canvas.style.margin = `${-pad}px`;
    orb.dpr = dpr; orb.pad = pad;
    if (interactive) grip(orb);
    draw(orb, 0);
    if (!manual) { orbs.add(orb); start(); }   // manual: the caller steps it (tests, stills)
    return orb;
  }

  const ease = (current, target, dt, rate) => current + (target - current) * (1 - Math.exp(-rate * dt));
  const REST_TILT = -0.32;
  const ORBIT_LEAN = -0.38;                                   // the constellation's circle, seen a little more from the side

  // Grab and spin: a round handle the size of the orb (its glow stays click-through, so buttons under it work).
  function grip(orb) {
    const {canvas, size} = orb;
    const handle = document.createElement("span");
    handle.className = "orb-grip";
    handle.style.width = handle.style.height = `${size}px`;
    canvas.replaceWith(handle);
    handle.append(canvas);
    handle.title = "Drag to spin";
    let lastX = 0, lastY = 0, lastT = 0;
    handle.addEventListener("pointerdown", (e) => {
      orb.held = true; lastX = e.clientX; lastY = e.clientY; lastT = performance.now();
      orb.vel = 0; orb.tiltVel = 0;
      handle.setPointerCapture(e.pointerId);
      handle.classList.add("held");
      e.preventDefault();
    });
    handle.addEventListener("pointermove", (e) => {
      if (!orb.held) return;
      const now = performance.now(), dt = Math.max(1, now - lastT) / 1000;
      const turn = (e.clientX - lastX) / (size * 0.5), lean = (e.clientY - lastY) / (size * 0.5);
      orb.spin += turn;
      orb.tilt = Math.max(-1.3, Math.min(1.3, orb.tilt - lean));
      orb.vel = ease(orb.vel, turn / dt, 1, 0.6);             // how fast it was moving when let go
      orb.tiltVel = ease(orb.tiltVel, -lean / dt, 1, 0.6);
      lastX = e.clientX; lastY = e.clientY; lastT = now;
      start();
    });
    const release = (e) => {
      if (!orb.held) return;
      orb.held = false;
      handle.classList.remove("held");
      if (performance.now() - lastT > 120) { orb.vel = 0; orb.tiltVel = 0; }   // held still before letting go
      if (Math.abs(orb.vel) > 0.05) orb.dir = Math.sign(orb.vel);
      try { handle.releasePointerCapture(e.pointerId); } catch (_) { /* already released */ }
    };
    handle.addEventListener("pointerup", release);
    handle.addEventListener("pointercancel", release);
  }

  // which small orbs the team job shows: the running ones first, then the finished; past six, five and the rest
  const whole = (v) => Math.max(0, Math.floor(Number(v) || 0));
  function planTeam(orb) {
    const counts = orb.teamOf ? orb.teamOf() : null;
    const running = counts ? whole(counts.running) : 1, total = running + (counts ? whole(counts.finished) : 0);
    orb.shown = Math.min(6, total);
    orb.restN = total > 6 ? total - 5 : 0;
    if (orb.restN) orb.label = orb.restN;                     // kept while the count fades out
    for (let k = 0; k < 6; k++) orb.runTo[k] = (orb.restN && k === 5 ? running > 5 : k < running) ? 1 : 0;
  }
  function layTeam(orb, fade, rate, dt) {
    const {shown} = orb;
    for (let k = 0; k < 6; k++) {
      const there = k < shown;
      if (there) {
        const to = (k / shown) * TAU;
        orb.ang[k] = orb.pres[k] < 0.02 ? to : ease(orb.ang[k], to, fade, rate);   // a new one starts in its place
      }
      orb.pres[k] = ease(orb.pres[k], there ? 1 : 0, fade, rate);
      orb.run[k] = ease(orb.run[k], there ? orb.runTo[k] : 0, fade, rate);
      orb.own[k] += dt * (0.15 + 0.45 * orb.run[k]);            // its own slow turn, quicker while it runs
      orb.turn[k] += dt * (1.4 + k * 0.2) * orb.run[k];         // and its rings
    }
    orb.rest = ease(orb.rest, orb.restN ? 1 : 0, fade, rate);
    orb.orbit = ease(orb.orbit, shown > 5 ? 0.98 : 0.86, fade, rate);
    orb.scale = ease(orb.scale, shown > 5 ? 0.25 : 0.3, fade, rate);
    orb.hubSpin += dt * 0.6;
  }

  function draw(orb, dt) {
    const {canvas, size, pad, dpr, dots} = orb;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    if (!orb.color) { orb.color = brandColor(canvas); orb.halo = orb.core = null; }
    const state = (orb.stateOf && orb.stateOf()) || "idle";
    const still = reduced.matches;
    if (still) dt = 0;
    // each look fades in and out, so a change of state is a movement, not a cut
    const fade = still ? 1 : dt;
    const rate = still ? 1e9 : 5;
    orb.think = ease(orb.think, state === "thinking" ? 1 : 0, fade, rate);
    orb.work = ease(orb.work, state === "working" ? 1 : 0, fade, rate);
    orb.team = ease(orb.team, state === "team" ? 1 : 0, fade, still ? rate : state === "team" ? 3.5 : 2.4);
    orb.done = ease(orb.done, state === "success" ? 1 : 0, fade, still ? rate : 3);
    orb.cube = ease(orb.cube, state === "stuck" ? 1 : 0, fade, state === "stuck" ? 6 : 4);
    orb.glow = ease(orb.glow, state === "success" ? 0.75 : state === "stuck" ? 0.7 : state === "idle" ? 0.35 : 0.55, fade, 2.5);
    // the constellation's layout, only while it shows: otherwise the last layout stays, to gather back from
    if (!orb.small && (state === "team" || orb.team > 0.001)) {
      if (state === "team") planTeam(orb);
      layTeam(orb, fade, still ? rate : 3, dt);
    }
    orb.t += dt;
    const turning = state === "working" ? 0.55 : state === "team" ? 0.35 : state === "stuck" ? 0.38
      : state === "success" ? 0.25 : state === "thinking" ? 0.2 : 0.16;   // radians a second
    if (!orb.held) {
      // a flick's momentum fades into the steady turn, in the direction it was thrown; the tilt drifts back
      orb.vel = ease(orb.vel, turning * orb.dir, dt, 1.4);
      orb.spin += dt * orb.vel;
      orb.tiltVel = ease(orb.tiltVel, 0, dt, 2.2);
      orb.tilt = Math.max(-1.3, Math.min(1.3, orb.tilt + dt * orb.tiltVel));
      orb.tilt = ease(orb.tilt, REST_TILT, dt, 0.5);
    }
    orb.ringAngle += dt * orb.work;

    const W = canvas.width, H = canvas.height, cx = W / 2, cy = H / 2, R = (size / 2) * dpr * 0.92;
    const [r, g, b] = orb.color;
    if (!orb.halo) {                                          // made once; each frame sets only how strong
      orb.fill = `rgb(${r},${g},${b})`;
      orb.halo = ctx.createRadialGradient(cx, cy, R * 0.2, cx, cy, R + pad * dpr);
      orb.halo.addColorStop(0, `rgba(${r},${g},${b},0.3)`);
      orb.halo.addColorStop(0.55, `rgba(${r},${g},${b},0.12)`);
      orb.halo.addColorStop(1, `rgba(${r},${g},${b},0)`);
      orb.core = ctx.createRadialGradient(0, 0, 0, 0, 0, 1);
      orb.core.addColorStop(0, `rgba(${r},${g},${b},1)`);
      orb.core.addColorStop(1, `rgba(${r},${g},${b},0)`);
    }
    ctx.clearRect(0, 0, W, H);
    const {think, work, cube, done, t, tilt, spin} = orb;
    const team = orb.small ? 0 : orb.team, orbiting = orb.small ? orb.team : 0;
    // waiting: the cube breathes (and its glow with it), and a band of light sweeps down it now and then
    const breath = 0.5 - 0.5 * Math.cos(t * TAU / 3.2);                       // 0..1, a breath every 3.2 s
    const sweep = ((t % 2.6) / 2.6) * 2.9 - 1.45;                              // the band's height, top to bottom
    // done: the shell breathes, slower and deeper, around its core
    const rest = 0.5 - 0.5 * Math.cos(t * TAU / 2.6);
    const glow = orb.glow * (1 + 0.35 * breath * cube) * (1 + (0.7 * rest - 0.3) * done);
    ctx.globalAlpha = Math.min(1, glow / 1.5);                // the glow behind the orb
    ctx.fillStyle = orb.halo;
    ctx.fillRect(0, 0, W, H);
    if (done > 0.01) {                                        // the core of light, shimmering
      ctx.globalAlpha = Math.min(1, (0.55 + 0.125 * Math.sin(t * 7)) * done);
      const k = R * (0.32 + 0.23 * rest);
      ctx.setTransform(k, 0, 0, k, cx, cy);
      ctx.fillStyle = orb.core;
      ctx.fillRect(-1, -1, 2, 2);
      ctx.setTransform(1, 0, 0, 1, 0, 0);
    }

    // this frame's turns, worked out once
    const cS = Math.cos(spin), sS = Math.sin(spin), cT = Math.cos(tilt), sT = Math.sin(tilt);
    const cubeX = 0.52, cubeY = Math.PI / 4;                 // seen from a little above, a corner towards you
    const qcY = Math.cos(cubeY + spin), qsY = Math.sin(cubeY + spin);
    const qcX = Math.cos(cubeX + (tilt - REST_TILT) * 0.6), qsX = Math.sin(cubeX + (tilt - REST_TILT) * 0.6);
    const swell = 1 + 0.045 * breath;
    for (let n = 0; n < orb.rings.length; n++) {
      const a = orb.ringAngle * orb.rings[n].speed;
      RING_C[n] = Math.cos(a); RING_S[n] = Math.sin(a);
    }
    for (let n = 0; n < SPARKS.length; n++) HEADS[n] = ((t * SPARKS[n].speed + SPARKS[n].phase) % TAU) - Math.PI;
    const cL = Math.cos(tilt + ORBIT_LEAN), sL = Math.sin(tilt + ORBIT_LEAN);
    if (team > 0.001) {                                       // where each small orb (and the hub) sits this frame
      for (let k = 0; k < 7; k++) {
        const hub = k === 6, p = hub ? 0 : orb.pres[k], a = hub ? 0 : orb.ang[k], far = orb.orbit * p;
        view(Math.cos(a) * far, 0, Math.sin(a) * far, cS, sS, cL, sL);
        CX[k] = P[0]; CY[k] = P[1]; CZ[k] = P[2];
        CS[k] = (hub ? 0.22 : 0.22 + (orb.scale - 0.22) * p) * (1 + 0.08 * P[2]);
        const own = hub ? orb.hubSpin : orb.own[k] + k;
        SPIN_C[k] = Math.cos(own); SPIN_S[k] = Math.sin(own);
        if (!hub) { TURN_C[k] = Math.cos(orb.turn[k]); TURN_S[k] = Math.sin(orb.turn[k]); }
      }
    }

    const {X, Y, Z, S, L, A, F} = orb;
    for (let i = 0; i < dots.length; i++) {
      const d = dots[i];
      let x = d.sx, y = d.sy, z = d.sz, lift = 0, lit = 0, dim = 1, grow = 1;
      if (d.wr >= 0 && work > 0.001) {                        // this ring turns on its own axis
        turnAxis(x, y, z, orb.rings[d.wr].axis, RING_C[d.wr], RING_S[d.wr]);
        x += (P[0] - x) * work; y += (P[1] - y) * work; z += (P[2] - z) * work;
        lift = 0.07 * work; lit = work;
      }
      if (think > 0.001) {                                    // sparks run along their circles, trails fading
        let spark = 0;
        for (let j = 0; j < d.syn.length; j += 3) {
          let behind = (HEADS[d.syn[j]] - d.syn[j + 1]) % TAU;
          if (behind < 0) behind += TAU;
          if (behind < TRAIL) { const f = 1 - behind / TRAIL; spark = Math.max(spark, f * f * d.syn[j + 2]); }
        }
        lift += 0.05 * spark * think;
        lit = Math.max(lit, 2 * spark * think);
        dim *= 1 - think * 0.45 * (1 - spark);                // the rest of the sphere stays calm
      }
      if (done > 0.001) {                                     // the shell breathes; faint bands drift up it
        const shell = 0.86 + 0.14 * rest + 0.03 * Math.sin(d.ring * 0.9 + t * 3);
        grow *= 1 + (shell - 1) * done;
        const band = Math.max(0, Math.sin(d.lat * 3 - t * 2.2));
        lit = Math.max(lit, band ** 6 * 0.8 * done);
        dim *= 1 - 0.2 * done;
      }
      if (orbiting > 0.001) { grow *= 1 - 0.18 * orbiting; dim *= 1 - 0.4 * orbiting; }
      const m = (1 + lift) * grow;
      view(x * m, y * m, z * m, cS, sS, cT, sT);
      x = P[0]; y = P[1]; z = P[2];
      let shade = z, big = 1;
      if (team > 0.001) {                                     // the dot's place in the constellation
        const k = d.k, hub = k === 6;
        let lx = d.sx, ly = d.sy, lz = d.sz;
        if (k === 5 && orb.rest > 0.001) { lx += (d.qx - lx) * orb.rest; ly += (d.qy - ly) * orb.rest; lz += (d.qz - lz) * orb.rest; }
        const running = hub ? 0 : orb.run[k];
        if (d.turn && running > 0.001) {                      // a running one turns its rings
          turnAxis(lx, ly, lz, SLOT_AXES[k], TURN_C[k], TURN_S[k]);
          lx += (P[0] - lx) * running; ly += (P[1] - ly) * running; lz += (P[2] - lz) * running;
        }
        const ox = lx * SPIN_C[k] + lz * SPIN_S[k], oz = -lx * SPIN_S[k] + lz * SPIN_C[k];
        view(ox, ly, oz, cS, sS, cL, sL);
        const there = hub ? 1 : orb.pres[k];
        const tLit = hub ? 0.25 : (d.turn ? 0.9 : 0.9 * (1 - running)) * there;
        const tDim = hub ? 0.9 : (1 - 0.3 * running) * there;
        x += (CX[k] + P[0] * CS[k] - x) * team;
        y += (CY[k] + P[1] * CS[k] - y) * team;
        z += (CZ[k] + P[2] * CS[k] - z) * team;
        shade += (P[2] * 0.6 + CZ[k] * 0.4 - shade) * team;  // each small orb keeps a little roundness of its own
        lit += (tLit - lit) * team; dim += (tDim - dim) * team;
        big += ((hub ? 0.6 : 0.5) - big) * team;        // finer dots: the small orbs read as orbs
      }
      let hush = 0;
      if (cube > 0.001) {                                     // fold into the cube, which turns as it waits
        const qx0 = d.cx * swell, qz0 = d.cz * swell;
        const qx = qx0 * qcY + qz0 * qsY, qz1 = -qx0 * qsY + qz0 * qcY;
        const qy = d.cy * swell * qcX - qz1 * qsX, qz = d.cy * swell * qsX + qz1 * qcX;
        x += (qx - x) * cube; y += (qy - y) * cube; z += (qz - z) * cube; shade += (qz - shade) * cube;
        const band = Math.exp(-((y - sweep) ** 2) / 0.03);    // the light passing down it
        lit = Math.max(lit * (1 - cube), (band * 1.25 + (d.edge ? 0.4 : 0)) * cube);
        hush = 0.4 * cube * (1 - band) * (d.edge ? 0.25 : 1); // the faces step back, so the light and edges show
      }
      // while it works, the dots that are not in a turning ring step back
      dim *= (d.wr >= 0 ? 1 : 1 - 0.55 * work * (1 - cube)) * (1 - hush);
      X[i] = x; Y[i] = y; Z[i] = z; S[i] = shade; L[i] = lit; A[i] = dim; F[i] = big;
    }
    if (orb.small) {                                          // the three bright dots circling a small orb
      let i = dots.length;
      for (let n = 0; n < ORBITS.length; n++) for (let k = 0; k < TAIL; k++, i++) {
        const o = ORBITS[n];
        const a = t * o.speed + o.phase - k * 0.11 * Math.sign(o.speed);
        turnAxis(o.u[0] * 1.18, o.u[1] * 1.18, o.u[2] * 1.18, o.axis, Math.cos(a), Math.sin(a));
        view(P[0], P[1], P[2], cS, sS, cT, sT);
        const head = k === 0, strength = (head ? 1 : (1 - k / TAIL) * 0.7) * orbiting;
        X[i] = P[0]; Y[i] = P[1]; Z[i] = P[2]; S[i] = P[2];
        L[i] = 1.4 * strength; A[i] = strength; F[i] = head ? 1.5 : 1;
      }
    }

    // far dots first: the order barely changes between frames, so an insertion sort in place is quick (and,
    // unlike a typed array's sort with a comparator, allocates nothing)
    const {order} = orb;
    for (let i = 1; i < order.length; i++) {
      const v = order[i], z = Z[v];
      let j = i - 1;
      while (j >= 0 && Z[order[j]] > z) { order[j + 1] = order[j]; j--; }
      order[j + 1] = v;
    }
    const base = Math.max(0.55, size / 64) * dpr, flat = 1 - 0.35 * cube;   // the cube keeps a little depth as it turns
    ctx.fillStyle = orb.fill;
    for (let o = 0; o < order.length; o++) {
      const i = order[o], near = Math.max(0, Math.min(1, (S[i] + 1) / 2)); // 0 at the back, 1 at the front
      const alpha = Math.min(1, (0.12 + 0.88 * near * near) * A[i] + L[i] * 0.3 * Math.min(1, A[i] * 4));
      if (alpha < 0.004) continue;
      const persp = 1 + (3.2 / (3.2 - Z[i]) - 1) * flat;
      ctx.globalAlpha = alpha;
      ctx.beginPath();
      ctx.arc(cx + X[i] * R * persp, cy - Y[i] * R * persp, base * (0.55 + 0.75 * near) * (1 + L[i] * 0.45) * F[i], 0, TAU);
      ctx.fill();
    }
    // the count of the helpers the sixth cluster stands for, shown while that cluster is at the front (drawn last,
    // it would float over a neighbour in front of it); a still frame keeps it
    const front = still ? 1 : Math.max(0, Math.min(1, (CZ[5] + 0.1) / 0.4));
    const shown = team * orb.pres[5] * orb.rest * front;
    if (shown > 0.02 && orb.label) {
      const persp = 3.2 / (3.2 - CZ[5]), px = cx + (CX[5] + CS[5]) * R * persp + 3 * dpr, py = cy - (CY[5] + CS[5] * 0.6) * R * persp;
      ctx.globalAlpha = 0.9 * shown;
      ctx.font = `600 ${Math.round(Math.max(10, size * 0.075) * dpr)}px system-ui, sans-serif`;
      ctx.textBaseline = "middle";
      ctx.fillText(`+${orb.label}`, px, py);
    }
    ctx.globalAlpha = 1;
  }

  function tick(now) {
    frame = 0;
    const dt = last ? Math.min(0.05, (now - last) / 1000) : 0;
    last = now;
    for (const orb of [...orbs]) {
      if (!orb.canvas.isConnected) { orbs.delete(orb); continue; }
      if (orb.canvas.offsetParent === null) continue;         // hidden: skip the drawing
      draw(orb, dt);
    }
    if (orbs.size && !document.hidden) frame = requestAnimationFrame(tick);
    else last = 0;
  }
  function start() { if (!frame && !document.hidden) frame = requestAnimationFrame(tick); }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { last = 0; start(); } });
  // the theme changed (light/dark): read the brand colour again
  if (window.matchMedia) {
    const scheme = window.matchMedia("(prefers-color-scheme: light)");
    (scheme.addEventListener ? scheme.addEventListener.bind(scheme, "change") : scheme.addListener.bind(scheme))(() => {
      for (const orb of orbs) orb.color = null;
    });
  }

  window.AgentxOrb = {create, step: draw, states: ["thinking", "working", "team", "success", "idle", "stuck"]};
})();
