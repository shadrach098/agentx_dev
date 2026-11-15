// The agentx orb: a sphere drawn as a matrix of dots, in the brand's lime, that shows what agentx is doing.
//   thinking  the surface bubbles, dots pushing out and settling back
//   working   a few rings of dots lift out and turn on their own axes, in opposite directions, like a mechanism
//   success   it settles into a slow turn with a warm glow (a turn just finished)
//   idle      the same slow turn, glow dimmed
//   stuck     the dots fold into a cube and hold still: agentx is waiting for you
// Drawn on a <canvas> with plain 2D maths (no library), one shared animation loop for every orb on the page.
// A large orb can be grabbed with the mouse (or a finger) and spun; let go and it keeps the flick's momentum,
// then settles into its slow turn in the direction it was thrown.
(() => {
  "use strict";
  const TAU = Math.PI * 2;
  const reduced = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : {matches: false};

  // Rings of dots, like lines of latitude: each ring is a row in the matrix.
  function sphereDots(rows) {
    const dots = [];
    for (let i = 0; i < rows; i++) {
      const lat = -Math.PI / 2 + ((i + 0.5) * Math.PI) / rows;
      const r = Math.cos(lat), y = Math.sin(lat);
      const n = Math.max(5, Math.round(rows * 2 * r));
      for (let j = 0; j < n; j++) {
        const lon = (j / n) * TAU + ((i % 2) * Math.PI) / n;
        dots.push({sx: r * Math.cos(lon), sy: y, sz: r * Math.sin(lon), ring: i});
      }
    }
    foldIntoCube(dots, rows >= 14 ? 7 : rows >= 10 ? 5 : 4);
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
  }

  // the rings that turn while it works: each on its own axis, alternating direction
  function workingRings(rows) {
    const picks = rows >= 14 ? [3, Math.floor(rows / 2), rows - 4] : [2, Math.floor(rows / 2), rows - 3];
    const axes = [[1, 0, 0], [0.5, 0, 0.87], [0, 0.4, 0.92]];
    return picks.map((ring, n) => ({ring, axis: axes[n], speed: (n % 2 ? -1 : 1) * (1.1 + n * 0.35)}));
  }

  function rotateAxis(x, y, z, [ax, ay, az], a) {            // Rodrigues' rotation about a unit axis
    const c = Math.cos(a), s = Math.sin(a), d = (ax * x + ay * y + az * z) * (1 - c);
    return [x * c + (ay * z - az * y) * s + ax * d, y * c + (az * x - ax * z) * s + ay * d, z * c + (ax * y - ay * x) * s + az * d];
  }
  function rotY(x, y, z, a) { const c = Math.cos(a), s = Math.sin(a); return [x * c + z * s, y, -x * s + z * c]; }
  function rotX(x, y, z, a) { const c = Math.cos(a), s = Math.sin(a); return [x, y * c - z * s, y * s + z * c]; }

  // "bubbling": waves through space at a few scales, so small patches of dots rise and settle in turn
  function bubble(x, y, z, t) {
    const n = Math.sin(5.3 * x + 1.9 * t) * Math.sin(4.7 * y - 1.4 * t + 1.3) * Math.sin(4.1 * z + 1.1 * t + 0.4) +
      0.7 * Math.sin(6.1 * (x - z) + 2.3 * t + 2.1) * Math.sin(5.6 * y + 1.7 * t) +
      0.4 * Math.sin(3.2 * (x + y + z) - 1.2 * t);
    const v = Math.max(0, n / 1.6);
    return v * v * v;                                         // only the tops of the waves: patches, not swells
  }

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

  function create(canvas, stateOf, {rows, manual, interactive} = {}) {
    const size = Number(canvas.dataset.size) || canvas.clientWidth || 32;
    rows = rows || (size >= 64 ? 18 : size >= 28 ? 11 : 8);
    const orb = {
      canvas, size, rows, stateOf, dots: sphereDots(rows), rings: workingRings(rows),
      t: 0, spin: 0.6, ringAngle: 0, think: 0, work: 0, cube: 0, glow: 0.4, color: null,
      tilt: REST_TILT, vel: 0, tiltVel: 0, dir: 1, held: false,
    };
    orb.ringOf = new Map(orb.rings.map((r) => [r.ring, r]));
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
      if (orb.cube > 0.5) return;                             // the cube holds still: it waits for you
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

  function draw(orb, dt) {
    const {canvas, size, pad, dpr} = orb;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    if (!orb.color) orb.color = brandColor(canvas);
    const state = (orb.stateOf && orb.stateOf()) || "idle";
    const still = reduced.matches;
    if (still) dt = 0;
    // each look fades in and out, so a change of state is a movement, not a cut
    const fade = still ? 1 : dt;
    const rate = still ? 1e9 : 5;
    orb.think = ease(orb.think, state === "thinking" ? 1 : 0, fade, rate);
    orb.work = ease(orb.work, state === "working" ? 1 : 0, fade, rate);
    orb.cube = ease(orb.cube, state === "stuck" ? 1 : 0, fade, state === "stuck" ? 6 : 4);
    orb.glow = ease(orb.glow, state === "success" ? 1 : state === "stuck" ? 0.7 : state === "idle" ? 0.35 : 0.55, fade, 2.5);
    orb.t += dt;
    const turning = state === "working" ? 0.55 : state === "thinking" ? 0.32 : 0.16;   // radians a second
    if (!orb.held) {
      // a flick's momentum fades into the steady turn, in the direction it was thrown; the tilt drifts back
      orb.vel = ease(orb.vel, turning * orb.dir, dt, 1.4);
      orb.spin += dt * orb.vel * (1 - orb.cube);
      orb.tiltVel = ease(orb.tiltVel, 0, dt, 2.2);
      orb.tilt = Math.max(-1.3, Math.min(1.3, orb.tilt + dt * orb.tiltVel));
      orb.tilt = ease(orb.tilt, REST_TILT, dt, 0.5);
    }
    orb.ringAngle += dt * orb.work;

    const W = canvas.width, H = canvas.height, cx = W / 2, cy = H / 2, R = (size / 2) * dpr * 0.92;
    ctx.clearRect(0, 0, W, H);
    const [r, g, b] = orb.color;
    // the glow behind the orb
    const halo = ctx.createRadialGradient(cx, cy, R * 0.2, cx, cy, R + pad * dpr);
    halo.addColorStop(0, `rgba(${r},${g},${b},${0.2 * orb.glow})`);
    halo.addColorStop(0.55, `rgba(${r},${g},${b},${0.08 * orb.glow})`);
    halo.addColorStop(1, `rgba(${r},${g},${b},0)`);
    ctx.fillStyle = halo;
    ctx.fillRect(0, 0, W, H);

    const tilt = orb.tilt;
    const cubeX = Math.atan(1 / Math.SQRT2), cubeY = Math.PI / 4;   // a cube seen corner-on
    const out = [];
    for (const d of orb.dots) {
      let x = d.sx, y = d.sy, z = d.sz, lift = 0, lit = 0;
      const ring = orb.ringOf.get(d.ring);
      if (ring && orb.work > 0.001) {                         // this ring turns on its own axis
        const [rx, ry, rz] = rotateAxis(x, y, z, ring.axis, orb.ringAngle * ring.speed);
        x += (rx - x) * orb.work; y += (ry - y) * orb.work; z += (rz - z) * orb.work;
        lift = 0.07 * orb.work; lit = orb.work;
      }
      if (orb.think > 0.001) {                                // the surface bubbles
        const bump = Math.min(1, bubble(d.sx, d.sy, d.sz, orb.t) * 1.8) * orb.think;
        lift += bump * 0.09; lit = Math.max(lit, bump);
      }
      [x, y, z] = rotY(x * (1 + lift), y * (1 + lift), z * (1 + lift), orb.spin);
      [x, y, z] = rotX(x, y, z, tilt);
      if (orb.cube > 0.001) {                                 // fold into the cube
        let [qx, qy, qz] = rotY(d.cx, d.cy, d.cz, cubeY);
        [qx, qy, qz] = rotX(qx, qy, qz, cubeX);
        const c = orb.cube;
        x += (qx - x) * c; y += (qy - y) * c; z += (qz - z) * c;
      }
      // while it works, the dots that are not in a turning ring step back
      const dim = ring ? 1 : 1 - 0.55 * orb.work * (1 - orb.cube);
      out.push([x, y, z, lit, dim]);
    }
    out.sort((a, c) => a[2] - c[2]);                          // far dots first
    const base = Math.max(0.55, size / 64) * dpr;
    for (const [x, y, z, lit, dim] of out) {
      const near = (z + 1) / 2;                               // 0 at the back, 1 at the front
      const persp = 1 + (3.2 / (3.2 - z) - 1) * (1 - 0.9 * orb.cube);   // the cube is drawn nearly flat: a clean hexagon
      const px = cx + x * R * persp, py = cy - y * R * persp;
      const alpha = Math.min(1, (0.12 + 0.88 * near * near) * dim + lit * 0.3);
      const rad = base * (0.55 + 0.75 * near) * (1 + lit * 0.45);
      ctx.fillStyle = `rgba(${r},${g},${b},${alpha})`;
      ctx.beginPath();
      ctx.arc(px, py, rad, 0, TAU);
      ctx.fill();
    }
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

  window.AgentxOrb = {create, step: draw, states: ["thinking", "working", "success", "idle", "stuck"]};
})();
