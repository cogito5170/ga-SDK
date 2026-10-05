# GA Console — design

GA Console is GA Engine's local operations console: `ga console` opens it on the user's Mac at 127.0.0.1 with a
one-time token, no login. It answers the questions the user keeps asking baseline, in plain Korean, in seven screens.
Its look follows gentleMonster's frontend grammar (`cogito5170/gentleMonster` docs/FRONTEND_ENGINE.md,
apps/worldplan): **paper, ink, hairline; one hot accent; one protagonist in empty space; nothing moves unless it is
alive.**

| file | what |
|---|---|
| `static/tokens.json` | W3C DTCG tokens, the source of truth |
| `static/tokens.css` | generated: `python -m ga.console.tokens` (`--check` in tests) |
| `golden/*.html`, `golden/golden.css` | the visual contract: one static page per screen, fake data in the API shapes |
| `judge.py` | `python -m ga.console.judge <file-or-url>`: the V checks on the rendered page, verdict JSON |

The frontend session implements `static/{index.html,app.css,app.js}` against the golden pages: same tokens, same
classes where it can, same hierarchy. A screen is accepted when the judge passes on it with real data at 375 and
1440 px. The golden pages are not the app; they have no fetching and no routing.

## 1. Material

- **Colour.** bg `#fbfaf7` (paper) · surface `#ecece9` (a raised plane: input wells, the log, bar tracks) · ink
  `#060c13` · sub `#63666a` (captions, secondary) · line `#b6b7b7` (hairlines; never text) · accent `#D9480F` ·
  accent-text `#cc440e` (on bg only — on surface it drops under 4.5) · on-accent `#000` (text on an accent fill).
  The rendered accent is **0.3–4 % of the page's pixels** at 1440 px (judge `accent`).
- **State.** Shape first, text always, colour last. Every state mark sits beside its word; a colour-blind reader and
  a screen reader lose nothing.

  | state | mark (`.mark.*`) | colour | words |
  |---|---|---|---|
  | ok / done / healthy | filled square | ink | 통합됨 · 응답 좋음 · 형식 맞음 · 깨끗함 · 합침 |
  | live / running | ring with a dot — **breathes** | ink | 도는 중 |
  | wait / unknown / draft | hollow ring | sub | 초안 · 보냄 · 확인 중 · 안 합침 · 안 읽음 |
  | off / stopped | short dash | line | 멈춤 · 응답 없음 |
  | fail / sent back | cross | accent | 실패 · 돌려보냄 · 형식 틀림 |

  A failing row gets the `state-fail-tint` background with **ink** text on it. The accent never colours body text
  of a failure; it marks it. Status colours are the existing inks plus the accent — no green, no yellow: the
  single hot accent rule holds.
- **Type.** Archivo → Helvetica Neue → Arial → Liberation Sans, then Apple SD Gothic Neo / Noto Sans KR for Hangul;
  mono JetBrains Mono → DejaVu Sans Mono → Menlo. No web fonts are loaded (0 external requests); the first installed
  family wins. Fluid modular scale `--step-m1 … --step-5`, ratio 1.175, 375 → 1440 px (16 → 19 px at step 0), plus
  `--mast` (44 → 96 px) for the protagonist. Captions are `--step-m1`, 600–700, uppercase, tracked `.12–.18em`.
  Nothing renders under 12 px at 375 (the smallest is mono at .88em of step-m1 = 12.5 px).
- **Space.** 8 px based, `--space-0 … --space-7` = 0.25, 0.5, 0.75, 1, 1.5, 2.5, 4, 6.5 rem. Sections are 4 rem
  apart; the masthead has 4 rem above it. Empty space is the material, not leftover.
- **Lines.** A 1 px ink rule under the top bar and over every section heading; 1 px `line` hairlines between items.
  The one 12 px accent rule (`.hot`) sits under the protagonist on every screen. Buttons are square-cornered, 1 px ink
  border, 44 px tall; the one primary button (accent fill, black text) exists only where something costs money.

## 2. Screen grammar

Every screen has the same skeleton:

```
top bar     GA CONSOLE · 127.0.0.1:8765 · 이 Mac 에서만          지금 작업 4 브랜치 서비스 3/4 토큰 묻기 결정
masthead    CAPTION · KEY FACT                                   (sub, tracked uppercase)
            THE PROTAGONIST                                      (h1, --mast, one per page)
            ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  (.hot, the accent)
            one plain line: what it means / what to do next      (lede; a term is explained here once)
sections    ─── SECTION CAPTION ─────────────── secondary fact    (h2: ink rule + tracked caption)
            items between hairlines, tables that turn into labelled cards under 720 px
footer      what this screen does not do (read-only, nothing leaves this Mac …)
```

The **protagonist** is the one answer the user came for — a sentence or a number — set big in empty space. It is
chosen by rule, not by taste: *the thing that needs the user; if nothing does, the thing that is alive; if nothing
is, the count.* The `.hot` rule is the accent's main use; the current nav item's underline and any fail mark are
the rest.

| screen | file | protagonist (h1) | then | breathes |
|---|---|---|---|---|
| 지금 | `now.html` | bridge state as a sentence: **브리지 살아 있음** / 브리지 멈춤 / 브리지 응답 없음 (+ last seen in the lede) | 한눈에 (open work, unread mail, services n/4) → 돌고 있는 일 (agv run, per-turn tokens, newest turn first) → 우편함 (latest 5: form, valid, from → to) | the bridge mark while it answers; the running turn |
| 작업 | `work.html` | the work item that needs the user (sent back, failed, reported and waiting to integrate), else **열린 작업 n** | 열린 작업: id, title (h3), one Korean line (`summary_ko`), the four-stop track 보냄 → 도는 중 → 보고됨 → 통합됨 (sent back / failed replace the last stop), meta: to, kind, tokens, BD-n links, branch → 끝난 작업 table | the running stop of a running item |
| 브랜치 | `branches.html` | the integration head of the current repo: **공용 머리 5558f39** | per repo: head subject, dirty, integration branch, ahead/behind → claude/* agv/* table: label_ko, merged mark + 합침/안 합침, ahead, behind, head, when | nothing (git is not alive) |
| 서비스 | `services.html` | **3 / 4 도는 중**, and the lede names the failed one | one item per service (api · worker · web · bridge): state + health marks, port, since, pid, buttons named with the service (멈추기 api) → the failed / selected service's log (stderr bold, `!`) with a plain-Korean hint under it | each running service's mark |
| 토큰 | `tokens.html` | **today's total tokens** | 오늘 나눠 보기 (agv turns, input, output, cost) → 작업마다 (bars, the number always written) → 장부 table (agv turn ledger + hub ledger) | nothing |
| 묻기 | `ask.html` | **무엇을 할까요?** | the box (ask = read only / do = may edit) → 비용 먼저 card: tokens, cost, model calls, what it will read → the only accent button **확인하고 돌리기** → 지난 물음 | the run while it runs |
| 결정 | `decisions.html` | the search result as a count: **“토큰” 5건**; with no query, **결정 215개** | search form → hits: BD-n, text with the match marked (`<mark>`: surface + ink underline, not yellow), the directives that cite it | nothing |

### States every screen has

- **Loading** — the masthead shows the last known value with the caption `불러오는 중 …` (sub). No spinner, no
  skeleton shimmer (both move without being alive). Under 300 ms show nothing new.
- **Empty** — the protagonist says the empty fact plainly (`열린 작업 없음`, `오늘 0 토큰`, `찾은 결정 없음`) and the
  lede says what would fill it (`ga send 로 지시를 보내면 여기에 보입니다.`). Never a blank page, never an illustration.
- **Error** — the protagonist becomes the error as a fact (`서버에 닿지 않음`), the lede the next step
  (`터미널에서 ga console 을 다시 여세요.`), with the fail mark. Technical detail goes in a `<details>` under it, mono.
- **Stale** — data older than 2 refresh cycles gets `n초 전 값` in the caption; marks of live things stop breathing
  (a frozen ring tells the truth: we do not know it is alive).
- **Long lists** — show the newest 20 (decisions, work, ledger), the newest 40 log lines, then a `더 보기` button.
  The page stays bounded (and the accent share stays measurable).

### Layout

One column under 960 px; at ≥ 960 px a `3fr / 2fr` pair (`.two`) for the screens with two peers (지금, 묻기). Max
width 1280 px, 16 px gutters. Tables become labelled cards under 720 px (`td[data-label]::before`), so no row
scrolls sideways; the nav wraps instead of scrolling. No text box may leave the viewport (judge `overflow-*`).

## 3. Korean copy rules

1. **Plain words first.** 통합 not 머지, 돌려보냄 not reject, 도는 중 not running, 공용 브랜치 not integration branch,
   머리 not HEAD, 기록 not log, 장부 not ledger. Ids, file names, commands and model names stay as they are, in mono.
2. **A term is explained once, where it first appears**, in the lede: `통합 = 공용 브랜치에 합침.` Not in a tooltip.
3. **Sentences end politely and short:** `~했어요 / ~합니다`; one idea per sentence; the lede is one or two sentences.
4. **Numbers are written, not drawn.** Every bar has its number; tokens use thousands separators (41,280); times are
   local `HH:MM` today, `MM-DD HH:MM` before, `어제` for yesterday. Unknown is `—` and the note says `— 는 0 과 다릅니다`.
5. **Buttons say verb + object:** `멈추기 api`, `확인하고 돌리기`, `비용 보기`. Never a bare `확인` / `OK`. A disabled
   button says why in its own label: `다시 켜기 브리지 (확인 중)`.
6. **Say what the user can do next**, not what went wrong inside: the worker log ends with `api 를 멈추고 worker 를 켠 뒤
   api 를 다시 켜 보세요.`
7. Korean spacing before particles on Latin words is kept (`worker 가`, `agy 와`), as the hub writes it.

## 4. Motion

- **Only living things move**: a running service, the live agv turn, the bridge while it answers. They *breathe*:
  `transform: scale(1 → .72 → 1)` over `--breath` (5.2 s), `--ease`. One property, no colour change, no opacity on text.
- A value that changes in place may cross-fade over `--reveal` (180 ms). Nothing slides in, nothing bounces, no
  scroll-driven effects in the console (it is a tool, not a walk).
- Every animation is declared **inside `@media (prefers-reduced-motion: no-preference)`**, and a
  `prefers-reduced-motion: reduce` rule sets `animation: none; transition: none` on everything. Under reduced motion
  nothing runs, not even the breath (judge `reduced-motion`: 0 running animations at 375 and 1440). Script-driven
  animation (Web Animations API) must check `matchMedia('(prefers-reduced-motion: reduce)')` itself — the CSS rule
  cannot stop it, and the judge will catch it.
- Polling or SSE never animates the page; a new mail item appears in place, the newest at the top.

## 5. The judge (V)

`python -m ga.console.judge <file-or-url> [--shots DIR]` opens the page in headless Chromium three times (375 and
1440 px with reduced motion, 1440 px with motion) and prints `{"pass", "V", "failed", "facts"}`; exit 0 pass, 1 fail,
2 skipped (no Playwright or no Chromium, with the reason). Checks: `overflow-375`, `overflow-1440`, `contrast`
(rendered: computed colour × ancestor opacity over the composited background, ≥ 4.5, ≥ 3 for large text; text over an
image or gradient is unmeasurable and fails), `min-font-375` (≥ 12 px), `offline` (0 requests outside the page's own
origin — `file:` for a file, scheme + host + port for a URL), `js-errors`, `names` (every control and image has an
accessible name), `headings` (`lang`, one h1, first heading h1, no skipped level), `reduced-motion`, `accent`
(0.3–4 % of pixels within distance 40 of `#D9480F`, full page at 1440), `weight` (all loaded bytes ≤ 512 KB).

What it does **not** judge: taste (no J score here, unlike gentleMonster's engine), text drawn on canvas, contrast of
text over images (the console has none), whether the copy is plain Korean (the review does), and keyboard order.
