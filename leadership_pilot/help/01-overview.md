# Leadership Pilot

Leadership Pilot helps a new IT product leader listen first and report honestly: it turns
listening-tour interviews into a ranked, cited problem list, and drafts the weekly leadership
update over status computed by rules.

## What it is for

- **Listening tour.** Interview notes become traced pain points, outcomes, and follow-ups;
  across interviews, themes, contradictions, personas, and a 30-60-90 readout.
- **Status and the weekly update.** Product status (schedule, scope, budget, risk) is
  computed from Backlog Pilot's roadmap, the RAID log, and budget actuals. The AI drafts the
  update; code checks every sentence before it can be approved and sent.

## Who does what

| Who | Does what |
| --- | --- |
| **The AI** | Extracts pain points with quotes, groups them into themes, suggests strategic fit, drafts personas, the readout, and the weekly update. |
| **Rules (code)** | Check every quote and cited label, count who raised what and rank problems, compute red/amber/green and warnings, and check every sentence of the update. |
| **The tour owner (approver)** | Reviews each extraction, overrides fit or severity with a reason, approves updates, and sends them. |
| **The audit log** | Records every extraction, acceptance, override, update version, delivery, and refused attempt. |

Interview notes are candid, so notes, interviews, the synthesis, updates, and the audit log
are for the tour owner and approvers only. Others can see the stakeholder map, status, and
the RAID log.

## A typical week

1. Add **Interviews** (paste, or upload text, Word, or a Teams transcript) and extract them.
2. Review each extraction and accept it into the stakeholder's record.
3. Run the **Synthesis**, check the ranking, and send problems to Backlog Pilot.
4. Refresh **Status**, update the **RAID log**, and draft the **Weekly update**.
5. Fix any flagged sentence, approve, and deliver.

The full list of rules and thresholds is in the [rules and settings reference](reference).
