# Tier 1 (MouseSearch port) — Frontend test plan

Manual UAT plan for the MAM economy bundle. Backend is covered by
automated tests; this plan exercises the user-visible paths that
can't be unit-tested.

Total test time ~20–30 min if everything works; add buffer for any
real-BP spends in Section B.

## Dry-run mode (BP-free testing)

On MamPage, Auto-buy: Upload credit → **Operator / testing** →
"Dry-run mode (simulate buys, spend no BP)". When on:

- All three bonusBuy wrappers return a synthetic success without
  hitting MAM.
- Audit rows are tagged `[DRY RUN]` so the history tile visibly
  separates them from real activity.
- Scheduler/router skip the shared-timestamp bump so flipping
  dry-run off later doesn't leave a phantom "just bought" lockout.

Run through Section B once with dry-run ON first to verify the UI
flow without spending BP. Then flip it off and do ONE real buy to
confirm end-to-end MAM integration. Sections C and D can stay in
dry-run mode the whole time (with the caveats noted in each
section).

## Capture real MAM responses (for future fidelity)

If you set `verbose_logging=true` in Settings, every real bonusBuy
response body is logged at DEBUG. Any live buy you do produces a
`seshat.mam.bonus_buy — bonus buy <label> raw response: {...}` line
in `docker compose logs seshat`. Paste those back and I'll update
the dry-run synthetic responses to match reality exactly — makes
future dry-run testing indistinguishable from a real MAM round-trip.

## Setup (once)

1. Start the dev server (`make dev` / `docker compose up` / whatever).
   Confirm `/api/v1/mam/status` returns your usual
   ratio/wedges/seedbonus — if that's broken the economy UI won't
   load either.
2. Open the **MamPage** in the browser. Confirm the cookie/stats
   section renders as before (regression check — commit 1's
   `seedbonus int→float` widening shouldn't have visibly changed
   anything).
3. Scroll down past the Stats/Account/Cookie Health sections. You
   should see **three new Sections + a dismissible "Auto-buy is off"
   banner**. If the banner is already hidden, your settings.json has
   `mam_economy_intro_dismissed=true` — fine to ignore.

## Section A — Config round-trip

4. In **Auto-buy: VIP**, toggle "Enabled" on. Reload the page. Toggle
   should stay on → config PUT round-trip works.
5. Change "Interval (hours)" to `12` (or any non-default). Blur out
   of the input. Reload. Value should persist.
6. Change "Weeks per buy" dropdown to `max`. Reload. Dropdown still
   shows `max`.
7. Turn the VIP enable toggle back off (cleanup).
8. In **Auto-buy: Upload credit**, toggle each of the three trigger
   rows individually. Confirm the threshold/chunk inputs in each row
   are dimmed (`opacity: 0.55`) when the trigger toggle is off, fully
   visible when on.
9. Under "Per-grab offers", toggle **both** "Show 'use wedge' checkbox"
   and "Show 'buy personal FL' checkbox" to **on**. Leave them on for
   Section C.

## Section B — Manual buys (real BP spend — do last/carefully)

> Skip this whole section if your MAM seedbonus is precious. The
> wrappers hit the real bonusBuy.php endpoint.

10. **VIP Buy now** → click. Expect a green-ish action banner:
    `"VIP buy OK — new seedbonus X"`. Scroll to **Auto-buy history**
    — a new row should appear with `action=VIP, trigger=manual,
    outcome=success`, with amount and cost populated. Your actual
    seedbonus at the top of the page should refresh on next
    minute-tick (or click the Refresh button in Cookie Health to
    force).
11. **Upload "Buy 1 GB"** → click. Same flow: action banner +
    history row with `amount=1`.
12. **Upload "Max affordable"** → click. History row with a much
    larger `amount` reflecting `floor(seedbonus / 500)`. Triple-check
    the seedbonus you had is close to what was spent (cost ≈ 500 ×
    amount).
13. **Rejection path**: set VIP min-bonus above your current seedbonus
    (e.g. 9999999), toggle VIP auto-buy on. Wait 60–90s for the
    scheduler tick. Refresh the history — an `insufficient_bonus` row
    should appear (the scheduler only audits at the interval boundary,
    and since `last_bought_at=0` the interval IS elapsed, so the
    decision engine writes the skip). If you see that row, the
    decision engine is wired correctly. Reset min-bonus to 0 when
    done.

## Section C — Per-grab wedge + personal-FL (BookSidebar)

Needs an unowned book that's "Found" on MAM.

14. Go to DiscBooksPage (or MAMPage). Pick a book with
    `mam_status=found` and open it in the sidebar. The "Send to
    pipeline" button should now be flanked by TWO checkboxes: **"Use
    wedge"** and **"Buy personal FL (50k BP)"**. (If checkboxes don't
    appear, go back to step 9 and confirm both per-grab offer toggles
    are on.)
15. Leave both unchecked. Click Send. Grab should submit normally.
    Check the pipeline/recent grabs page for the new grab.
16. Pick a different book. Check **only "Use wedge"**. Click Send.
    Check qBit WebUI → the new torrent should be there AND the wedge
    pool (shown on MamPage top stats) should have decreased by 1.
17. Pick a different book. Check **only "Buy personal FL"**. Click
    Send. Go to Auto-buy history: a `personal_fl, user_grab, success`
    row should appear with 50000 BP cost.
18. **Buy failure path**: manually edit settings.json (or use the
    `/economy/config` PUT) to temporarily set a scenario that would
    make MAM reject — there isn't a clean toggle for this, so either
    test with depleted BP or skip. If you test: expected a
    `personal_fl, user_grab, failure` row AND the inject still
    completes normally (the grab isn't blocked by the PFL buy
    failing).

## Section D — Buffer gate

**BP-free testing approach**: you don't need a 10 GB+ torrent or
lots of BP. Two tricks:

- Set the safety margin to something absurd (e.g. `5000` GB). That
  guarantees ANY non-free torrent triggers the gate regardless of
  real size. Testing just the banner math + dispatch path.
- Turn on Dry-run mode before doing step 21. The fake buy succeeds,
  the retry's preflight still sees the unchanged real buffer, so it
  re-blocks. That's expected — you're verifying the button fires +
  audit records, not that the grab actually goes through under dry-
  run. (In real mode with a reasonable margin, the retry succeeds.)

19. On MamPage, enable **Buffer gate** toggle under Auto-buy: Upload
    credit → "Buffer gate (pre-download)". Set safety margin to
    `5000` GB. Turn Dry-run mode on under Operator / testing.
20. Pick any non-free book via BookSidebar that's `mam_status=found`
    and click Send to pipeline. Expected: the send does NOT complete
    — the **BufferInsufficientBanner** renders at the bottom of the
    sidebar showing real size + buffer + shortfall + a "Buy N GB (X
    BP) & retry" button with sensible math.
21. Click **"Buy N GB & retry"**. Expected:
    - a toast: `Upload buy OK — new seedbonus [unchanged]`
    - a new history row `upload, manual, success` tagged
      `[DRY RUN]`
    - the banner re-appears (preflight still sees unchanged buffer
      since dry-run doesn't actually move MAM state)
    Click Cancel to dismiss.
22. Turn Buffer gate back off AND safety margin back to `1` AND
    Dry-run mode back off when done.
23. **IRC autograb block (optional, requires IRC to be receiving)**:
    temporarily set the safety margin to `5000` again with dry-run
    OFF. Wait for an IRC announce for a non-free torrent. Check
    Auto-buy history — you should see a `buffer_gate_block,
    irc_autograb, buffer_gate_block` row with a message like
    "Would need X GB; buffer is Y GB". If you have ntfy configured
    you should also get a push: "Buffer gate blocked a grab.
    Further blocks suppressed for 6h." Subsequent IRC blocks
    within 6h should NOT push (but still audit).

## Section E — Intro banner + cleanup

24. If the intro banner is still showing at the top, click "Got it".
    Reload — banner gone. Edit settings.json to set
    `mam_economy_intro_dismissed=false` and reload → banner returns.
25. **Final cleanup**: turn off everything you enabled for testing
    (all auto-buy enables, buffer gate, per-grab offers). Confirm
    state matches what you want running in prod.

## Known limitations / things NOT to test here

- There are no frontend unit tests. TS type-check: run `npm run
  typecheck` locally if you have node installed.
- The scheduler's 60s wake cadence means auto-buy tests take up to a
  minute to fire. Don't assume "nothing happened" after 10 seconds.
- Upload trigger priority (ratio → buffer → bonus) is backend-only
  and can't really be confirmed visually without either hitting the
  auto-buy for real or inspecting logs. The backend tests cover this
  exhaustively.

## If something is broken

Tell the assistant the section number + what you saw. Backend logs
are the first place to look (these loggers are at INFO by default):

- `seshat.orchestrator.economy_scheduler`
- `seshat.routers.economy`
- `seshat.orchestrator.dispatch`
- `seshat.mam.bonus_buy`
