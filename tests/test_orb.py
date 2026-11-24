"""The orb (agentx_dev/resources/webui/orb.js) stepped by hand under node, with a stub canvas: every state draws
without errors at every size, the team constellation included, with 1, 6 and 20 helpers. Skipped without node."""
import json
import shutil
import subprocess

import pytest

from agentx_dev.cli.webui import RESOURCES

HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(process.argv[1], "utf8");
const reducedFlag = {matches: false};
globalThis.window = globalThis;
window.devicePixelRatio = 1;
window.matchMedia = (q) => q.includes("reduced-motion") ? reducedFlag : {matches: false, addEventListener() {}};
globalThis.document = {hidden: false, addEventListener() {}, body: {append() {}},
  createElement: () => ({style: {}, remove() {}, append() {}, addEventListener() {}, classList: {add() {}, remove() {}}})};
globalThis.getComputedStyle = () => ({getPropertyValue: () => "#b8ff3e", color: "rgb(184, 255, 62)"});
globalThis.requestAnimationFrame = () => 1;
vm.runInThisContext(src);

function stubCanvas(size) {
  const ctx = {
    arcs: 0, bad: [], texts: [], _fill: "",
    get fillStyle() { return this._fill; },
    set fillStyle(v) { if (/NaN|undefined|Infinity/.test(String(v))) this.bad.push("fillStyle " + v); this._fill = v; },
    set globalAlpha(v) { if (!Number.isFinite(v)) this.bad.push("globalAlpha " + v); },
    get globalAlpha() { return 1; },
    font: "", textAlign: "", textBaseline: "",
    clearRect() {}, fillRect() {}, beginPath() {}, fill() {}, save() {}, restore() {}, setTransform() {},
    translate(x, y) { if (!Number.isFinite(x) || !Number.isFinite(y)) this.bad.push("translate"); },
    scale(x, y) { if (!Number.isFinite(x) || !Number.isFinite(y)) this.bad.push("scale"); },
    arc(x, y, r) { this.arcs++; if (![x, y, r].every(Number.isFinite) || r < 0) this.bad.push(`arc ${x} ${y} ${r}`); },
    fillText(t, x, y) { this.texts.push(t); if (!Number.isFinite(x) || !Number.isFinite(y)) this.bad.push("fillText"); },
    createRadialGradient(...a) {
      if (!a.every(Number.isFinite) || a[2] < 0 || a[5] < 0) this.bad.push("gradient " + a);
      return {addColorStop: (o, c) => { if (/NaN|undefined/.test(c)) this.bad.push("stop " + c); }};
    },
  };
  return {ctx, canvas: {dataset: {size: String(size)}, clientWidth: size, style: {}, width: 0, height: 0,
    isConnected: true, offsetParent: {}, getContext: () => ctx}};
}

const out = {states: window.AgentxOrb.states, runs: []};
const sizes = [220, 128, 32, 16];
const teams = [[1, 0], [6, 0], [3, 3], [20, 0], [2, 18], [0, 0]];
for (const reduced of [false, true]) {
  reducedFlag.matches = reduced;
  for (const size of sizes) for (const [running, finished] of teams) {
    for (const state of window.AgentxOrb.states) {
      const {ctx, canvas} = stubCanvas(size);
      let now = state;
      const orb = window.AgentxOrb.create(canvas, () => now, {manual: true, teamOf: () => ({running, finished})});
      for (let i = 0; i < 240; i++) window.AgentxOrb.step(orb, 1 / 60);
      const arcsOne = ctx.arcs;
      // and on into every other state, so each look fades out into the next
      for (const next of window.AgentxOrb.states) { now = next; for (let i = 0; i < 30; i++) window.AgentxOrb.step(orb, 1 / 60); }
      now = state;
      for (let i = 0; i < 180; i++) window.AgentxOrb.step(orb, 1 / 60);
      ctx.texts.length = 0;                       // settled back into the state: what it draws now, over a
      let labelled = 0;
      for (let i = 0; i < 60; i++) {                                    // whole turn (the count shows at the front)
        const before = ctx.texts.length;
        window.AgentxOrb.step(orb, 0.35);
        if (ctx.texts.length > before) labelled++;
      }
      out.runs.push({reduced, size, running, finished, state, bad: ctx.bad.slice(0, 5), arcs: arcsOne,
                     texts: [...new Set(ctx.texts)], labelled});
    }
  }
}
// no teamOf: the team look still draws (one helper running)
{
  const {ctx, canvas} = stubCanvas(128);
  const orb = window.AgentxOrb.create(canvas, () => "team", {manual: true});
  for (let i = 0; i < 120; i++) window.AgentxOrb.step(orb, 1 / 60);
  out.noTeamOf = {bad: ctx.bad.slice(0, 5), arcs: ctx.arcs};
}
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def stepped():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "-e", HARNESS, str(RESOURCES / "orb.js")], capture_output=True, text=True,
                          timeout=240, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_orb_knows_the_team_state(stepped):
    assert stepped["states"] == ["thinking", "working", "team", "success", "idle", "stuck"]


def test_every_state_steps_by_hand_without_errors(stepped):
    runs = stepped["runs"]
    assert len(runs) == 2 * 4 * 6 * 6
    for run in runs:
        assert not run["bad"], run
        assert run["arcs"] > 0, run
    assert not stepped["noTeamOf"]["bad"] and stepped["noTeamOf"]["arcs"] > 0


def test_the_constellation_caps_at_six_with_a_count_for_the_rest(stepped):
    def texts(size, running, finished, reduced=False):
        return next(r["texts"] for r in stepped["runs"] if r["state"] == "team" and r["size"] == size and
                    r["running"] == running and r["finished"] == finished and r["reduced"] == reduced)
    for reduced in (False, True):
        assert texts(220, 20, 0, reduced) == ["+15"]           # 5 shown, one cluster for the other 15
        assert texts(128, 2, 18, reduced) == ["+15"]
        assert texts(220, 6, 0, reduced) == [] and texts(220, 3, 3, reduced) == [] and texts(220, 1, 0, reduced) == []
        # small orbs never draw the constellation (nor its count): a few bright dots circle the sphere instead
        assert texts(32, 20, 0, reduced) == [] and texts(16, 20, 0, reduced) == []
    # no other state draws text
    assert all(not r["texts"] for r in stepped["runs"] if r["state"] != "team")


def test_the_count_shows_while_its_cluster_is_at_the_front(stepped):
    """Drawn last, the "+N" would float over a small orb in front of its cluster: it shows while the cluster is at
    the front of the turn and fades as it goes round the back; a still frame (reduced motion) keeps it."""
    def labelled(reduced):
        return next(r["labelled"] for r in stepped["runs"] if r["state"] == "team" and r["size"] == 220 and
                    r["running"] == 20 and r["reduced"] == reduced)
    assert 10 < labelled(False) < 50, labelled(False)
    assert labelled(True) == 60
