# dailies interview scenarios

Raw user inputs for the interview live-test, used by `scripts/simulate_interviews.py`.
Verbatim — typos preserved, because these are *raw user inputs*. Prescriptive details
(e.g. "a spreadsheet") are incidental: the interview maps them onto dailies' own `ddl`
state, not a literal external tool. The driver parses each `## Scenario N` block as one
input.

## Scenario 1 — Devon Rex breeder scouting

every day, spawn a subagent to do extensive deep research on new devon rex breeders in Canada or the US that are not on our list. Finding them is haldf the challenge, the other half is doing a very thorouhg review of how ethical and high wuality they are. we care way  more about quality than price. find the forums and reviews hidden deep in the corners that give us the real story on there. then if they pass our bar, add them to the list we are tracking (have 1 tab of the spredsheet just tracking breeders, dont do a deep dive on breeders already on our approved list). aim to add ~25 breeders on the first run, then 1-2 a week from there on. keep quality bar high. for each approved breeder on our list, spawn a subagent that uses its own chrome group to look at that breeders site and get the latest info on its kittens, both ones listed (track all the relevant characteristics) and new details about ones to come vs our last visit. we want to be proactive here, so if one of them says you have to reach out or fill out a form to get more details, or anything changes, then go ahead and do it with my info (and cc rebecca if its an option or its us sending an email). track everything in the spreadsheet, adjusting the schema as needed.

## Scenario 2 — Samsung OLED + Sonos price scout

Daily price scout for the Samsung 77" S95F matte OLED (model QN77S95FAFXZA) + Sonos Arc Ultra (black), shipped to San Francisco 94102 (tax rate 8.625%). Goal: find the cheapest *delivered, in-stock, all-in* total and alert only on a new record low.

## Scenario 3 — Flight credit chaser

Every day, use the flighty MCP to keep track of any new flights I have taken. also every day, if it has been >14 days since I took any flight and I have not received credit for it yet (use the airline logins in the onepassword vault), submit a form/email request with ervything the airline requires, monitor for follow ups and respond as needed.

## Scenario 4 — Marriott stay credit chaser

same as the flight one, but for marriott hotel stays

## Scenario 5 — Accountability buddy for Rebecca

accountability buddy for rebecca. every weekday at 10am, check if she is awake (8sleep data) and doing work (desktop integration that takes screenshots on demand). if she's not, play on the bedroom speaker using an 11labs voice (via the MCP) to warm her. if she's not working by 11, add 1 to her penalty counter. for every hour _before_ 10am she is up working ona  weekday, remove one from the penalty counter. every day of the week where her location is at home at noon or 7pm, and nothing is on the calendar at that time, if there is a penalty count, order a random salad from sweetgreen or mixt na dhave it delivered to the house, charged to her card, on doordash, and decrease the penalty count. every sat at 11am if she has accumilated no penalties that work week, order her a random pie from tartine. send her transactional updates via the bluebubbles server.
