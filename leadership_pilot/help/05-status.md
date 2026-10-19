# Status and RAID log

Status is computed by rules from facts, so it can be checked and cannot be argued into
green.

## How status is computed

For each product, four dimensions: **schedule** (roadmap moves, capacity load, blocked work),
**scope** (new work added), **budget** (spend against plan), and **risk** (the RAID log).
Each red or amber opens to show the rule and the numbered facts (F-1, F-2, ...) that
triggered it. A product's overall status is its worst dimension. Early warnings flag slips,
budget burn, orphaned high risks, and unconfirmed dependencies.

## Inputs

- **Backlog Pilot's roadmap and change log**, refreshed through the suite API (or a demo
  snapshot offline).
- **The RAID log**, edited here: risks, issues, assumptions, and dependencies with impact,
  probability, owner, and date.
- **A budget CSV** of plan and actuals by month.

The exact rules and thresholds are in the [reference](reference#status-rules).
