# Demo call role-play

Open **Demo Console → Demo call guide**, then press **Call my phone** when ready. You play the dispatcher at **Sierra Valley Transport**. This is a fictional brokerage scenario; the phone call is real. Another demo can start after the previous call finishes.

These are expected responses for the scenario, not a record of verified live tool execution. The call gathers facts and asks for missing evidence; it cannot approve or send a payment.

## Your case

| Fact | Demo value |
|---|---|
| Load | GG-1042 |
| Invoice / bill of lading | SVT-1042 / BOL-GG-1042 |
| Route | Oakland, California → Sacramento, California |
| Fictional companies | Broker: Golden Gate Freight; shipper: Bay Pantry Foods, Oakland warehouse; receiver: Riverbend Grocery Distribution, Sacramento DC |
| Delivery appointment | October 23, 2026, 9:00 a.m. PDT |
| Freight | 18 pallets of packaged food, dry van, 36,000 lb |
| Driver times | Checked in 8:45 a.m.; released 12:15 p.m. PDT |
| Detention terms | Two free hours from the 9:00 a.m. appointment, then $100/hour |
| Carrier invoice | $975 linehaul + $250 claimed detention = $1,225, unpaid |
| Documents present | Signed proof of delivery (POD), signed rate confirmation, unsigned detention log |
| Missing evidence | Signed detention receipt supporting the billed time/charge |

The free period ends at 11:00 a.m. Release at 12:15 p.m. leaves **75 billable minutes × $100/hour = $125 detention**. The claim exceeds that calculation by $125. The early 8:45 arrival does not move the agreed free-time start before 9:00. This calculation is provisional pending signed detention support; it is not a payment approval.

## What to say

| Your line | Expected agent response |
|---|---|
| “Which load is this? Where was it going?” | Identifies GG-1042, Oakland to Sacramento, and Sierra Valley Transport. It should not invent addresses or additional shipment facts. |
| “What was the appointment time and what are you disputing?” | States October 23, 2026 at 9:00 a.m. PDT, the $250 detention claim, and the provisional $125 calculation awaiting signed backup. |
| “Driver checked in at 8:45 and left at 12:15. We billed $250 detention.” | Applies two free hours from the appointment, calculates 75 minutes/$125, and requests the signed detention receipt. It should not accept the $250 solely because you say it was billed. |
| “What paperwork do you already have?” | Names the signed POD, signed rate confirmation, and unsigned detention log; identifies the missing signed detention receipt. |
| “Where can I send it?” | Says no email or upload channel is configured for the demo. It should not invent an address, link, or claim it received a document. |
| “Has this invoice been paid?” | Uses the read-only payment lookup and reports the $1,225 invoice as unpaid. It should not approve, promise, or initiate payment. |

You can use the prompts in any order. If a lookup is unavailable, the agent should say so rather than claim it checked. The inbox and call status are the evidence for what actually ran; the guide describes the intended conversation.
