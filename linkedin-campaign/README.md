# agentx-dev LinkedIn campaign — 4 weeks

A launch-to-momentum plan for `agentx-dev` on LinkedIn. Twelve posts across
four weeks, three per week (Mon / Wed / Fri). Each post is written to be
copy-pasted, with notes on media, hashtags, and how to work the comments
in the first hour.

## Weekly arc

| Week | Theme | Goal | Emotional beat |
|------|-------|------|----------------|
| 1 | Awareness | People learn who you are and that this exists | Curiosity |
| 2 | Depth | Prove it is technically real, not a wrapper | Respect |
| 3 | Utility | Show what people can build with it today | Desire |
| 4 | Community | Convert readers into stars, users, contributors | Belonging |

## Posting cadence

- **Monday 8:00–10:00 AM (your local time)** — main post of the week, the
  "meaty" one. Best reach window on LinkedIn.
- **Wednesday 8:00–10:00 AM** — the mid-week beat: usually a code
  snippet or a lesson.
- **Friday 11:00 AM–1:00 PM** — the lighter, community-oriented post.
  Friday afternoons get low reach; keep it before lunch.

Do not post more than 3× per week. LinkedIn punishes over-posting by
compressing the reach of every subsequent post.

## Rules that apply to every post

1. **Hook line first.** LinkedIn cuts the post at ~210 characters on
   mobile. Everything you need people to see must be above that fold.
2. **No links in the body.** Put the GitHub / PyPI link in the *first
   comment* of your own post, right after posting. Bodies with links get
   downranked hard.
3. **Native code beats screenshots for engagement, screenshots beat
   nothing.** LinkedIn now supports code blocks — use them.
4. **Comment on your own post within 5 minutes** with the link + a
   pinned reply asking one specific question. That comment often gets
   more reach than the post itself.
5. **Reply to every comment in the first hour.** The algorithm reads
   dwell time and reply density in the first 60 min as the main signal.
6. **Do not use hashtags in the body.** Put 3–5 at the end, one line,
   lowercase. `#python #ai #llm #opensource #agents`.
7. **Tag sparingly.** Only tag people who genuinely engage with your
   content — a bad tag kills reach.

## What NOT to do

- Do not repost the same content in different words. LinkedIn detects
  it and shadow-limits repeat posters.
- Do not @ Anthropic, OpenAI, or any big account hoping for a repost —
  it reads as a plea.
- Do not lead with "Excited to announce..." — it is the most
  algorithmically penalized opener on the platform.
- Do not add disclaimers ("Not a marketer, but..."). Just post.

## Metrics to track

Track these weekly in a spreadsheet — not for vanity, for signal.

| Metric | Where to find it | What it tells you |
|--------|-------------------|-------------------|
| Impressions per post | Post analytics | Whether the hook worked |
| Reactions ÷ impressions | Analytics | Whether the body landed |
| Comments per post | Post | Whether people care enough to engage |
| Profile views week-over-week | Profile analytics | Whether the brand is compounding |
| PyPI downloads day-of-post vs baseline | `pypistats` | Actual conversion |
| GitHub stars gained per post | GitHub insights | Actual conversion |

If a post gets 3× your average impressions, write a follow-up in that
direction next week. Signal to noise ratio matters more than volume.

## How to adapt this plan

- **Something breaks and you need to skip a week?** Drop Wednesday, not
  Monday. Monday is the highest-reach slot.
- **A post takes off?** The next week, do a "part 2" that goes deeper.
  Never do part 2 the same week — cadence matters.
- **A post flops?** Do not delete it. Reply to your own comment with a
  clarification and move on. Deleted posts penalize your reach for a
  week.

## Files

```
linkedin-campaign/
  README.md                     ← this file
  week-1-awareness/
    01-mon-announcement.md
    02-wed-origin-story.md
    03-fri-live-demo.md
  week-2-depth/
    01-mon-multi-agent-handoffs.md
    02-wed-rag-in-15-lines.md
    03-fri-streaming-and-traces.md
  week-3-utility/
    01-mon-research-agent.md
    02-wed-pr-reviewer.md
    03-fri-support-triage.md
  week-4-community/
    01-mon-lessons-learned.md
    02-wed-honest-comparison.md
    03-fri-whats-next.md
```
