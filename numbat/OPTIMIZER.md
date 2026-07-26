# How Numbat decides what to do

This guide explains, in plain language, how Numbat's optimizer thinks — why it
charges when it charges, why it sometimes sits still, and why it occasionally
does something that looks odd until you see the prices it's looking at. It's
written for any home battery setup; the examples use round numbers, not any
particular brand of hardware.

If you just want to configure Numbat, the Settings page and [DOCS](DOCS.md)
cover every field. Read this when you want to understand the *why* — or when
the plan surprises you.

---

## The big picture

Every 5 minutes, Numbat asks one question:

> **Looking at the next ~36 hours of prices, solar and household usage —
> what sequence of battery actions gives the cheapest overall power bill?**
>
> (We'll call that 36-hour window the *horizon*.)

It solves that as a single planning problem (the same kind of maths used for
delivery routes and factory schedules), gets back a complete 36-hour plan,
then **acts only on the first 5 minutes of it**. Five minutes later it plans
again from scratch with fresh prices. So the "plan" you see on the dashboard
is not a promise — it's today's best guess, continuously revised. When a
price spike is confirmed, Numbat re-plans within seconds rather than waiting
for the next tick.

Everything below describes what "cheapest overall" means in detail, because
that's where all the interesting behaviour comes from.

## What the optimizer weighs

The optimizer adds up, over the whole 36 hours:

1. **Money spent importing** — every kWh drawn from the grid, at that
   interval's buy price.
2. **Money earned exporting** — every kWh sent to the grid, at that
   interval's feed-in price.
3. **Battery wear** — a small cost for every kWh the battery discharges
   (see [Wear cost](#wear-cost) below).
4. **The value of energy still in the battery at the end** — the
   [hold value](#the-hold-value), so the plan doesn't treat leftover charge
   as worthless.
5. **Penalties for breaking soft promises** — the daily full-charge target
   and the spike reserve are "soft": the plan can break them, but it pays a
   configured penalty when it does, so it only breaks them for something
   genuinely better.

It then picks the plan with the lowest total. That's the whole trick. Charge
timing, export decisions, curtailing (deliberately spilling) solar on
negative feed-in days — none of
those are special-cased rules; they all just fall out of "lowest total".

Physics is respected throughout: charging and discharging lose a few percent
each way (your configured efficiencies), the battery has power limits and a
minimum reserve, and your grid connection has import/export limits.

## The hold value

**The problem it solves:** if leftover charge were worth nothing, the
cheapest 36-hour plan would always end with an empty battery — the optimizer
would sell every stored kWh at any price above the wear cost before the
horizon closed, like a shop dumping stock before closing time. That's wrong,
because your battery keeps running after the plan ends.

So Numbat gives every kWh still stored at the end of the horizon a value: the
**hold value**, shown in the "More info" panel on the dashboard.

**How it's set (on "auto"):** stored energy is valued at what it would cost
to put back — the *cheapest* upcoming import price, plus charging losses. If
power will be 8c at some point tonight, a stored kWh is worth about 8.4c
(8c ÷ 95% charge efficiency): sell it for less and you've traded a full
battery for a loss.

Two guard rails:

- **A floor** (default 1c/kWh): even on a day when prices crash, stored
  energy is never valued at zero — that would let the battery sell out for
  pennies.
- **A flat-day cap:** when prices are flat all horizon, the "rebuy" logic
  would value stored energy slightly *above* what it saves you, and the
  battery would sit full while you import. The hold value is capped so a
  flat day still runs the house from the battery.

**The scaling knob** (default 100%) multiplies the auto value: above 100%
the battery holds charge more stubbornly; below 100% it trades more freely.
(It can't lift the value past the flat-day cap — flat days stay sensible.)

> **Pitfall — scaling above 100% causes buy-to-stockpile imports.** At 110%
> the model believes stored energy is worth 10% *more* than it costs to buy
> back — so importing at any price within ~10% of the cheapest upcoming
> window looks like free money, and the plan will grid-charge just to hold
> the energy. A single "value of stored energy" can't make the battery
> holdier without also making acquiring more attractive. If you see
> unexplained imports in cheap windows, check this knob first; prefer the
> export spread or the daily target for "keep more in the tank" behaviour.

> **Pitfall — setting a fixed hold value.** You can replace "auto" with a
> fixed number, but a too-high value makes the battery hoard (import to run
> the house while "saving" its charge), and a too-low one brings back
> end-of-day selling. "Auto" tracks the market; fixed numbers go stale.

## Wear cost

Every kWh the battery discharges shortens its life a little. The wear cost
prices that: it's charged against **every discharged kWh** in the plan.

A realistic number is the cost of the *wearing part* divided by the energy
it will deliver — and it's easy to overstate. Dividing a full installed
price by cycle throughput (say $16,000 for 32 kWh with 8,000-cycle cells:
16,000 ÷ 256,000 kWh ≈ 6c) charges cycling for the inverter, wiring and
labour, which don't wear — and at a typical cycle a day the cells reach
calendar end-of-life years before they run out of cycles, so extra cycling
mostly consumes life the calendar was going to take anyway. The honest
basis is a future **cell-stack replacement**, at cell prices that keep
falling, spread over the cells' cycle throughput: ~$150 per kWh of cells
over 8,000 cycles is about 2c/kWh. Priced that way, most lithium systems
land around **half a cent to 3 cents per kWh**. Numbat ships with a default
of 3c — the cautious top of that range — so compute your own number and
lower it if yours is smaller.

> **Pitfall — using wear cost to stop cheap exports.** It's tempting to
> raise the wear cost to mean "don't cycle the battery unless it's really
> worth it". But wear applies to *all* discharge — **including running your
> own house**. Set it to 10c+ and the battery will sit idle holding stored
> solar while you import at 18c, because "18c import minus 10c wear" no
> longer looks attractive. Keep wear at the honest physical number and
> express "only sell when it's worth it" with the
> [export spread](#when-does-it-sell) instead.

## When does it sell?

The battery sends stored energy to the grid only when the feed-in price
beats the value of keeping it. Roughly:

```
sell when:   feed-in price  >  hold value (+ losses)  +  wear cost  +  your margin
```

That last term is the **minimum battery export spread** (default 5c; 0 = off).
It's your personal "worth getting off the couch for" threshold — the minimum
*profit* per exported kWh, after wear is paid and the energy is valued at
its rebuy cost. If cycling your battery for less than 5c/kWh profit isn't
worth it to you, that's the default; set 0 to sell whenever marginally
profitable. Feed-in spikes sail over any sensible spread, so spike revenue
is unaffected.

There's also a blunter tool: **minimum battery export price**, a hard floor.
Below it the battery never sells, full stop (it still runs the house, and
solar still exports). Use it if you want a guarantee in dollars rather than
a margin.

Both only restrict *selling stored energy*. Solar export, charging, and the
battery covering your house are never blocked by them.

**Example — the thin-margin sell.** Feed-in is 12.7c. The hold value is
8.4c (cheap power coming tonight), wear is 2c. Selling nets roughly
12.7 − 8.8 − 2 ≈ 1.9c per kWh — real profit, and with the spread set to 0
Numbat would take it, shuffling ~40 kWh through your battery for a dollar
or two. The default 5c spread lets this one pass: 1.9c is under the bar,
so the charge stays put until a fatter margin comes along.

## Import reluctance (optional)

The optimizer treats forecast prices as certain: a predicted 26c sell
tonight justifies a certain 20c import now, at any positive margin. If
you're warier than that — forecasts change, the import is real money — the
**import reluctance** setting (default 5c; 0 = off) adds a virtual toll to every
imported kWh *in the planning maths only* (the dashboard's dollar figures
stay real). Import-dependent bets then need bigger margins, and the plan
leans toward solar and stored energy. Genuine spikes clear any sensible
toll, and it's skipped when the buy price is negative — getting paid to
charge stays attractive. Side effect: grid top-ups toward the daily target
pay the toll too, so you may need a slightly higher target penalty.

## The daily full-charge target

Left to pure economics, the optimizer charges the battery *just enough* for
the forecast. Surprise usage and unforecast price spikes are worth nothing
to it — it can't see them. On a mild day it may happily stop at 50%.

If you want the battery full every afternoon as insurance, set the **daily
full-charge target** (e.g. 100% at 3pm). Two things make it work:

- **It's a floor held through the evening** (default 4 hours), not a single
  instant — so the battery is full *for* the evening peak, not just at one
  moment it could immediately sell out of.
- **It's soft, with a price.** The penalty (default 10c per kWh, per hour of
  shortfall) is your maximum willingness-to-pay to be full. Anything cheaper
  than the penalty gets done (topping up from cheap solar or a cheap grid
  window); anything dearer doesn't. A genuine feed-in spike still outbids
  it — which is what you want.

> **Pitfall — a penalty below your evening prices.** The penalty is paid
> per hour short, so with the default 4-hour hold a 10c penalty means being
> short is worth at most ~40c/kWh to avoid. If topping up the last few kWh
> means importing at 50c, the battery will (correctly, by its lights) stop
> short of the target. Either raise the penalty, or set the
> **daily target price multiple**, which
> automatically keeps the penalty above the going price level (it uses
> whichever is higher: your fixed penalty, or the multiple × the median
> upcoming import price). A multiple of 2–3 is plenty.

> **Pitfall — expecting the target to stop cheap dumping.** If tomorrow's
> feed-in goes negative (a solar glut), refilling tomorrow is nearly free —
> so the battery can dump tonight *and still meet* tomorrow's target. The
> target can't prevent that; the export spread or price floor is the right
> tool there.

## The spike reserve

When the price forecast shows a possible spike in the next few hours (a
forecast feed-in above a threshold — default $1/kWh — within the lookahead,
default 4 hours), Numbat softly reserves energy (default 6 kWh) so there's
something to sell if the spike confirms. Like the daily target, it's soft: a better opportunity can
break the reserve, but it costs the configured penalty. When a spike
actually confirms, Numbat re-plans within seconds and discharges into it.

## Trust and hygiene features

A few quieter mechanisms keep the plan sensible:

- **Sell price forecast haircut** (default off): optionally discounts
  above-average feed-in forecasts more than 6 hours away, for price feeds
  that habitually over-promise distant spikes. Leave it off if your price
  source already provides tempered predictions (Amber's advanced predicted
  pricing does).
- **Action switch threshold** (default $0.02): the current action only
  changes if the new plan beats sticking with the old action by more than
  this, across the whole horizon — this stops the battery flip-flopping
  between near-identical plans every 5 minutes.
- **Honest state of charge:** if the battery reports below its planning
  reserve (e.g. after a sensor recalibration), the plan starts from the real
  number rather than pretending the reserve energy exists.
- **Stale-data safety:** if prices stop updating, Numbat stops trusting them
  rather than planning on fiction, and your inverter's failsafe keeps it in
  plain self-consumption.

## Worked examples

Round numbers throughout; your prices will differ.

**1. Cheap overnight power.** Overnight buy price 8c, tomorrow evening 35c.
Charging at 8c, losing ~10% round trip, gives energy that displaces 35c
imports — roughly 24c profit per kWh once losses and wear are paid. The plan grid-charges overnight and runs
the house on battery through the evening. If overnight went *negative*
(−2c), you'd see it charge harder: it's being paid to fill up.

**2. A normal sunny day.** Solar covers the house from 9am, surplus fills
the battery, the rest exports at 5c. Evening comes, the battery runs the
house (worth 30c+/kWh in avoided imports — easily beats 2c wear), and the
leftover is held, valued at tomorrow's cheapest refill price. Nothing
exciting — which is the point.

**3. A forecast feed-in spike at 6pm.** The forecast shows $1.20 feed-in
for two half-hours tonight. The plan pre-charges through the afternoon
(even importing at 20c — the spread to $1.20 dwarfs losses and wear), holds
until 6pm, then discharges at full power into the spike, exporting
everything above house load. If the spike never confirms, the re-plans melt
the position back into ordinary evening self-consumption — you're left with
a full battery before the peak, which was cheap insurance.

**4. Negative feed-in at midday.** Feed-in drops to −3c (you'd *pay* to
export). Solar beyond what the house and battery can absorb is curtailed —
spilled rather than exported at a loss. Import prices near zero may also
make it a great time to fill the battery.

## Setting up a different system: where to start

1. **Get the physical numbers right first**: capacity, charge/discharge
   power limits, efficiencies (95% each way is typical), and your grid
   import/export limits. Everything downstream depends on these.
2. **Set an honest wear cost** — replacement cost ÷ lifetime throughput,
   usually 0.5–3c/kWh. Resist making it a behaviour knob.
3. **Give it your load sensor** so the house forecast is learned from
   reality. Without one, Numbat plans as if your house uses nothing.
4. **Let it run in dry-run and watch the "More info" panel** for a
   few days. Every decision shows the prices and hold value behind it.
5. **Then add preferences**: an export spread if thin-margin cycling annoys
   you; the daily target if you want full-battery insurance; the spike
   settings if your tariff has real spikes.
6. **Use Test mode before changing live settings.** Synthetic scenarios show
   how a setting behaves in specific conditions; time travel replays real
   recorded days from your own system with your candidate settings.

## Quick reference: which knob for which itch

| "I wish it would…" | The knob |
|---|---|
| stop selling for tiny profits | Min battery export spread |
| never sell below X c/kWh, full stop | Min battery export price |
| be full every evening | Daily full-charge target (+ hold hours) |
| try harder to actually reach that target | Daily target penalty / price multiple |
| cycle less overall | Wear cost — but keep it honest (0.5–3c); see pitfalls |
| never charge the battery from the grid | Allow grid charging (turn it off) |
| prefer solar/stored energy over importing | Import reluctance |
| hold charge more / trade more | Hold value scaling |
| stop chasing forecast spikes it can't trust | Sell price forecast haircut |
| keep something in the tank for possible spikes | Spike reserve settings |
| plan for a hungrier house than the forecast | Load forecast buffer |
