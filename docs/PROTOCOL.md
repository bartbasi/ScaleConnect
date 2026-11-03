# ScaleConnect protocol notes

For the driver. Written in our own words from packet captures and tests on the test scale. No addresses, no real product data and no vendor code belong in this file.

**Status, November 2, 2026.** The read is found and works, and can freeze a scale (section 3). Delete is done by overwriting the id with a placeholder, and Bart checked that at the test scale. A delete message probably exists too, but no test has shown that it removes anything, and it is never sent (section 7). Sections marked "not tested" are still open.

## Confidence labels

| Label | Meaning |
|---|---|
| **Bart** | Confirmed at a scale by Bart |
| **Reply** | The scale's reply only. Nobody looked at the display |
| **Reply, read back** | The scale's reply, and a later read returned the same data. The read shows what is stored, not what the display or a label does with it |
| **Inferred** | Our reasoning from evidence. Not tested directly |

Nobody could see the test scale's display during the lab. No result from the lab is **Bart** unless it says so.

## Evidence pointers

Private files on the lab PC. They never go in the repo.

| Short name | File |
|---|---|
| notebook | `research/lab-notebook.md`. Section names below are its time headings |
| lab log | `research/lab/lab-log.jsonl`. Exact bytes of every lab send and reply, by run name |
| first dump | `backups/front-dump-20261101-1754/`. The first full read |
| prompt | `SCALECONNECT_PROMPT.MD`, "What we know" |
| p1 capture | `captures/lab-phase1-unknowns.pcapng`. All lab traffic |
| test1 to test10 | `captures/test1-*` to `captures/test10-*`. ScaleLink and early tests. test9 and test10 hold full sends |

## Summary

| Job | How | Confidence |
|---|---|---|
| Read all items | Send `LP`, then `Ok` after each block, until `LP00End` | Reply, read back |
| Add or change an item | `bX01XX`, `GP` batches, `GPF0` | Bart |
| Delete an item | Overwrite the id with a placeholder record, using the add or change sequence. Never send `EP` | Bart |
| Change a PLU | Same id with a new Code and PLU, using the add or change sequence. The old PLU stops working | Bart |
| Identify a scale | `CX` | Reply |

## 1. Transport

- TCP port 50036. A full TCP scan of the test scale found only this port. UDP was not scanned. (Reply. prompt)
- An unknown message makes the scale close the connection with no reply, in about 0.02 to 0.05 s. A wrong check character does the same. So does unknown plain text. It happens inside a `bX01XX` session too. (Reply. notebook "5:50 PM", lab log `p01_baseline`, `p02_round1`)
- So a close with no reply means rejected, not "try again".
- Every message and reply so far is single byte text. The scale stores and returns raw bytes. Treat them as Latin-1 when decoding so nothing is lost. (Reply, read back. first dump, id 491)

## 2. Framing

### Messages to the scale

Text, then TAB, LF, one check character, CR. (Bart. Matched every framed message captured.)

Check character: for each byte from the first through the LF, multiply by a weight of 2, 3, 4, 5, 6, 7, 8, repeating. Take the negative of the total, mod 11. Add 0x30. A value of 10 comes out as `:`.

Test vectors. `\t`, `\n`, `\r` are TAB, LF, CR.

| Text | Framed |
|---|---|
| `RI` | `RI\t\n4\r` |
| `CX` | `CX\t\n0\r` |
| `LP` | `LP\t\n6\r` |
| `Ok` | `Ok\t\n7\r` |
| `bX01XX` | `bX01XX\t\n1\r` |
| `GPF0` | `GPF0\t\n8\r` |
| `EP` | `EP\t\n9\r` |
| One record | `GP01485\t9992\t9992\tTEST ITEM TWO\t1.02\t\t1\t1\t0\t0\t0\t0\t2026-11-01 13:31:00\t0\t0\t0\t0\t0\t0\t0\t\n:\r` |

### Replies from the scale

Replies use the same check, with two odd habits. Every one of 239 logged replies fits these rules. (Reply. `research/lab/verify_log.py`)

**A reply can have an echo in front.** A reply to `GP` looks like `5\rOk\t\n2\r`. The first `5\r` is the check character of the message just received. The last check character covers everything before it, including the echo.

**A reply can have a chain of check characters at the end.** A read block ends in `...\t\n1\r7\r`. The `1` is the check of the block. The `7` is the check of the block plus `1\r`. If the scale sends the block again, the chain grows, as in `1\r7\r8\r:\r`. (Reply. notebook "5:51 PM")

Rules for the parser:

- Do not stop at the first CR. Read to the end of the reply as the message type defines it.
- Do not assume one reply per TCP segment. Allow for a reply to arrive in pieces.
- If leftover `x\r` pairs show up in front of the next reply, skip them. This was not seen, because the lab read until quiet, but it follows from the chain. (Inferred)
- To check a reply, walk its CR positions. The character before each CR must be the check of all bytes before that character. A leading echo pair has nothing before it, so skip it.

Why the echo happens, as far as we can tell: the scale reads `count` lines from the message and leaves the rest in the buffer it replies from. When the count was bigger than the lines sent, no echo came back. (Inferred. notebook "Correction and study of the EP timing")

## 3. Read items: `LP`

**A read can freeze a scale.** Seen four times, always the same way: the scale's TCP acknowledges the message, no reply comes, my FIN is never acknowledged, and the scale drops off the network until it is rebooted. The scale can keep weighing while this is happening.

| Scale and date | State of its items | Where it stopped |
|---|---|---|
| FRONT, Nov 1 | Right after `EP` tests | First block |
| FRONT, Nov 2 | Damaged. The read before it gave blank records | First block |
| FRONT, Nov 2 | After a product reset, with one item at id 65 | First block |
| COUNTER, Nov 2 | Old records, not written since an import renumbered the items | Asking for ids 41 to 45 |

Reads worked every time on tables that a full send had just written, with ids 1 to N all filled: FRONT at 484 and 500 records, KITCHEN at 485, COUNTER at 485. Empty slots after the last item are fine. The last block is short and then `LP00End` comes.

COUNTER's freeze was cured by rewriting ids 1 to 484 with no reset. What was wrong with its old records is unknown. A scale fresh from a product reset must be reloaded with writes only before anything reads it. (Reply, capture. notebook "November 2")

No `RI` or `bX01XX` needed. Connect and go.

1. Send `LP\t\n6\r`.
2. The scale replies with a block: `LP05`, then 5 records, each ending in TAB LF, then the chain. The two digits are the number of records.
3. Send `Ok\t\n7\r`. The scale replies with the next block.
4. Repeat until the reply is `LP00End\t\n4\r`. Then close.

(Reply, read back. notebook "5:53 PM", first dump)

A real block with test items only:

```
LP05496\t9111\t9111\tTEST PRICE WHOLE\t5\t\t1\t1\t0\t0\t0\t0\t2026-11-01 13:38:47\t0\t0\t0\t0\t0\t0\t0\t\n497\t9112\t9112\tTEST TIPO ZERO\t2.00\t\t0\t1\t0\t0\t0\t0\t2026-11-01 13:39:01\t0\t0\t0\t0\t0\t0\t0\t\n498\t9108\t9108\tTEST DUP PLU\t3.00\t\t1\t1\t0\t0\t0\t0\t2026-11-01 13:39:17\t0\t0\t0\t0\t0\t0\t0\t\n499\t12345\t12345\tTEST PLU FIVE\t1.00\t\t1\t1\t0\t0\t0\t0\t2026-11-01 13:39:33\t0\t0\t0\t0\t0\t0\t0\t\n500\t123\t123\tTEST PLU THREE\t1.00\t\t1\t1\t0\t0\t0\t0\t2026-11-01 13:39:48\t0\t0\t0\t0\t0\t0\t0\t\n0\r6\r
```

What two full reads showed:

- 500 records, ids 1 to 500, in id order, 20 fields each. The two reads compared were identical. (Reply, read back. notebook "5:53 PM", `research/lab/dumps/after-dp/`)
- Ids 1 to 484 match the full send from ScaleLink field by field, all 484. ScaleLink sends in PLU order, so the scale keeps items by id, not by the order they came. (Reply, read back. test9 stream 7, `research/lab/compare_dump.py`)
- The record format is the same as in `GP`, so a read record should be able to go back as is. (Inferred. Not tested.)
- Each block's first byte came 0.003 to 0.13 s after the request, median 0.085 s. No long waits, unlike writes. (Reply. `research/lab/verify_log.py`)
- At the lab's pace of one send a second, a full read takes 100 s. ScaleLink sends at reply speed on writes. A read at reply speed should take about 10 to 20 s. (Inferred. Not tested, because lab limits kept one send a second.)

Other behavior:

- `LP` ignores anything after the two letters. `LP01`, `LP97`, `LP485`, `LP\t485`, `LPX` and others all start at id 1. We found no way to start in the middle or read one item. (Reply. lab log `p08_lp_params`)
- Bare `L` closes. (Reply)
- A message other than `Ok` during a read makes the scale send the same block again. Seen with `CX`, once. (Reply. lab log `p02_round1`)
- If the PC closes during a read, the scale sends the block again, then `LP00End`, then closes. A new `LP` starts at id 1. (Reply. p1 capture stream 47)
- Not seen yet: a block with fewer than 5 records, or a gap in the ids. The table was full. Trust the count in the block, not the number 5. (Unknown)

**A read that stopped on bad order, November 2, 16:55.**

Seen, on FRONT over WiFi, with nobody touching it. A read stopped about 20 s into a 30 s read, judging by timing, because a block failed our order check. FRONT answered ping at once and read clean about 3 minutes later. KITCHEN and COUNTER took the change, so FRONT was left one change behind. We kept no ids, so we can't say what FRONT sent. (Reply)

Not proven: the scale resent a block because an `Ok` was late on WiFi. It fits the resend above, but nobody has seen a late `Ok` cause one. (Inferred, weakly)

Also that day: KITCHEN failed a read at 14:23 with no freeze, and the reason was lost. KITCHEN timed out at 15:03 with a possible freeze, and we don't know how far it got. So a read now names the ids in this failure and logs every problem. It can also read once more, only after this one kind of failure, and only with a setting that is off by default. On the test scale, a stopped read followed by a whole read 5 seconds later passed 20 of 20. Later, on November 3, a scale failed one read with blocks out of order, and a second read run by hand straight after passed. (Read problem log, November 3.) The library's own repeat has not run on a real scale yet. Other odd answers do not count: the blank records in the table above came right before a freeze. See `docs/LIBRARY.md`, "When a read goes wrong". The next time this happens, that log says whether the block was an exact repeat.

Bart reports ScaleLink's own read hangs. We have no capture of it. Our read did not hang in two full reads and a dozen partial ones.

Caution: the test scale left the network 9 s after the delete probes, on the next `LP`. It had just done two full reads with no trouble, so the delete probes are the likely cause, not the read. That is not proven. See section 7.

## 4. Other reads

All framed, on a fresh connection. None of these are needed for version 1.

| Message | Reply | Probably | Confidence |
|---|---|---|---|
| `LC` | `LC00End` | Folders, empty | Reply |
| `LI` | `LI00End` | Additional info, empty | Reply |
| `LM` | `LM00End` | Messages, empty | Reply |
| `LO` | `LO00End` | Offers, empty | Reply |
| `LV` | `LV00End` | Agents, empty | Reply |
| `LH` | `LH15` and 15 lines of text, alignment, number | Headers | Reply |
| `LT` | `LT01` and one line of label texts | Screen and ticket texts | Reply |
| `LG` | `LG01` and one line of general settings | **Do not use.** It holds values that look like passwords. Lab stopped there | Reply |

After the one block of `LH`, `LT` or `LG`, a framed `Ok` closes the connection. (Reply. lab log `p05_l_family`)

Closed with no reply, so unknown: `LA LB LD LE LF LJ LK LL LN LQ LR LS LU LW LX LY LZ`. (Reply)

## 5. Identify and clock

**`CX`.** Reply: `CX00`, then serial, capacity, step, language, an empty field, unit, model, a number, each ending in TAB, then LF, check, CR. The last number was 2, and ScaleLink's database has 2 for decimals. The connection stays open. (Reply. lab log `p01_baseline`. Meaning of the last number is Inferred.)

`C` ignores the second letter. `CP` gets the same reply. (Reply)

**`SetHORA,hh:mm:ss` and `SetFECHA,dd/mm/yyyy`.** Plain text ending in LF, with no TAB and no check character. Reply `OK` and LF. ScaleLink sends them after `CX` in its connection test. We did not send them. (Reply. `captures/scale-5min-20261031.pcapng`)

## 6. Write items

The sequence ScaleLink uses:

1. Connect. Send `RI\t\n4\r`. The scale closes with no reply.
2. Connect. Send `bX01XX\t\n1\r`. Reply `bXXXOk\t\n7\r`.
3. Send a batch: `GP`, a two digit count, then the records, separated by TAB LF, framed. Wait for the reply before the next batch.
4. Send `GPF0\t\n8\r`. Reply `GPF0Ok\t\n5\r`. Close.

(Bart. test1 to test7)

Reply to a batch is `<echo>\rOk\t\n<check>\r` or `<echo>\rError\t\n<check>\r`. **Check this reply on every batch.** `GPF0Ok` comes back either way. (Reply. test8 stream 1 has an `Error` followed by `GPF0Ok`.)

What is proven:

- ScaleLink's 484 items in batches of 5 were stored exactly as sent. (Reply, read back)
- Our own code changed a price on all three scales, and only the targeted scale changed when one was targeted. (Bart)
- A new item uses the same message with a new id. (Bart)
- Same id with a new PLU, description and price replaces the item in place. The old PLU disappeared on all three scales. (Bart)
- Same id with a new Code and PLU works, and the old PLU stops working. PLU and Code are always the same 4 digit number. (Bart, November 2. See section 7.)
- **Id decides which item is replaced, not Code or PLU.** Two ids hold the same Code and PLU side by side. Id 485 holds the last Code sent, and its old Code is gone from the read. (Reply, read back. first dump, ids 485, 493, 498) What the scale does when staff enter a PLU held by two ids is unknown.

Ids:

- Above 500 gets `Error`. This happened when the table had room, so the limit is on the id number, not just on how many items. (Reply. test8 for ids 600 and up, prompt for the 500 and 501 boundary.) ScaleLink's message file also says 500 products for this kind of scale.
- Id 500 stores. (Reply, read back)
- Id 0 gets `Ok` but never appears in a read. Do not use it. It may be gone or hidden. (Reply, read back. Bart needs to check if its PLU exists on the display.)
- All 500 ids were in use at every read, so a read of a scale with free ids has not been seen.

Values the scale stored without complaint, all returned unchanged by a read: a 100 character name, raw Latin-1 accents, `& % # ' ( )`, price `1000.00`, price `5`, price `0.01`, 3 and 5 digit Code and PLU, duplicate PLU. (Reply, read back. first dump, ids 486 to 500) The only `Error` cause seen so far is an id above 500. **The scale checks almost nothing, so ScaleConnect must.** What the display and labels do with these values is unknown.

### What `RI` does

It gets the same treatment as an unknown message: a close with no reply in about 0.03 s. Unknown messages close in about 0.02 to 0.05 s. So we can't tell if it does anything at all. (Reply. lab log `p02_round1`) It may do nothing on this model. (Inferred) A `GP` session without `RI` has not been tested. Keep sending it, as ScaleLink does, until that test runs.

### What `bX01XX` does

The reply takes about 1.04 s, sometimes 0.27 to 0.75 s. The other two messages with a slow reply and an `XXX` in it are `S` and `D` below. After a session of `bX01XX` with no `GP` and no `GPF0`, all 500 records were still there. Ids 485 to 500 also lived through two full sends with `bX01XX` and `GPF0`. So it does not erase items. (Reply, read back. lab log `p01_baseline`, first dump. Also Bart.) A `GP` without it, and other values in place of `X01XX`, have not been tested. Keep sending it.

### What `GPF0` does

Reply is immediate. After an accepted item plus `GPF0`, the scale does not answer new connections for about 10 to 11 s. After a session where the item got `Error`, there is no such gap. (Reply. test8 streams 58, 60, 62. `captures/scale-5min-20261031.pcapng` stream 3.) ScaleLink's message file mentions the scale processing information after sending completes, which may be this. (Inferred) A `GP` without `GPF0` has not been tested. Always send it.

## 7. Delete: a placeholder, and never `E`

**A delete overwrites the id with a placeholder record. The library never sends any `E` message, to any scale.**

### The placeholder

Replacing an id removes the old PLU, so this needs no message of its own. It is an ordinary `GP` record in the sequence in section 6. For id N:

| Field | Value |
|---|---|
| Id | N |
| Code and PLU | 9000 + N, so 9001 to 9500 |
| Description | `UNUSED` |
| Price | `0.01` |
| Field 6 | Empty |
| Fields 7 to 12 | `1 1 0 0 0 0` |
| Time | When it was sent |
| Fields 14 to 20 | All `0` |

Tested on FRONT on November 2, 2026. Through the library's own send and read, ids 485 to 487 went from item records to placeholders and back, and each read back exactly, such as `486, 9486, 9486, UNUSED, 0.01`. Bart then checked at the scale, and all six PLUs came out as expected. (Bart)

Staff look items up only by PLU, so a placeholder does no harm. It does use up the slot until an add writes over it. (Inferred)

### `E`: what the probes showed

These tests are why `E` stays unused. They are all our data. No more `E` tests without a fresh decision from Bart.

After the `EP` tests below, FRONT dropped off the network, then froze within seconds of every reboot until its products were factory reset. Either those messages or the odd test records stored on it caused that. We can't separate the two.

All framed, on fresh connections, aimed at a throwaway item. (Reply. notebook "6:00 PM" and "Correction and study of the EP timing", p1 capture streams 61 to 67.)

| Sent | Reply | Time |
|---|---|---|
| `EP` | `EP\t\nError\t\n7\r` | 0.001 s |
| `EX`, `E`, `E1` | closed | |
| `EP500` | `Ok\t\n7\r` | 6.44 s |
| `EP01500` | `2\rOk\t\n8\r` | 2.18 s, seemingly queued behind the one above |
| `EP\t500` | `Ok\t\n7\r` | 0.58 s |
| `EP01\t500` | `6\rOk\t\n0\r` | 0.21 s |
| `EP123` | `Ok\t\n7\r` | 1.22 s |
| `EP500\t123\t123` | `Ok\t\n7\r` | 6.81 s |
| `EP01500\t123\t123\t` and the rest of a full record | `8\rOk\t\n7\r` | 2.53 s, seemingly queued behind the one above |

What we think, all Inferred:

- `EP` is some operation on products, with the same shape as `GP`: a two digit count, then lines. Time per count is about 0.12 s, as with `GP`.
- It is probably the delete. ScaleLink's message file lists removing products from a scale, and `E` fits the Spanish word for delete. Our best guess is `EP01` then an id.

What we don't know:

- **Whether it removes anything.** After these messages the next `LP` got no reply and the scale left the network. `Ok` came back for every shape, including nonsense. So `Ok` to `EP` proves nothing.
- Whether the entry is an id, a Code, a PLU or a full record.
- Whether it needs `bX01XX` first, or `GPF0` or something else after.
- What a read shows after a delete.
- Whether it is safe. The last frame from the test scale came 9 s after the last `EP` session. Since then it has not answered connections, ping or ARP. Two of the shapes had a count of 50 with almost nothing to read. Twice a session started while the previous one was still being worked on. Either may be the cause. Neither is proven. A driver must never send a wrong count, and must wait for the reply.

ScaleLink's own Delete and Purge showed no `E` message in test9 and test10. So we have no capture of a real delete.

A full send never removes anything. Ids 485 to 500 were not in ScaleLink's two full sends and were still on the test scale afterward. So that is why an item purged from ScaleLink stays on a scale. (Reply, read back. first dump, test9.)

### `S` and `D`

| Sent | Reply | Time |
|---|---|---|
| `SP` | `SXXX\t\n7\r` | 1.58 s |
| `DP` | `DXXX\t\n4\r` | 0.22 s |

The connection stays open and `CX` works after. A full read after `DP` was identical to the one before, and the first full read came after `SP`. So neither changed items. (Reply, read back.) Meaning unknown. ScaleLink's message file mentions requesting sales and details from a scale, which may be these. (Inferred, weakly.) The driver has no reason to send them.

### Tried and closed, so unknown

`ZZ`, `RP`, `TP`, `OP`, `QP`, `BP`, `XP`, plain `GetHORA`. (Reply)

## Ids can differ between scales

The scale replaces a record by id. ScaleLink's import appears to renumber products, because its file has no ids (`SCALELINK_FILE.md`). A scale that misses the next full send keeps the old order. Each one item send then lands on another product. Seen on COUNTER on November 2: same products up to id 24, different products from id 25, and one product confirmed lost. Never assume a catalog id matches a scale. Read that scale's own record first. A full send of ids 1 to N puts the scale back in step. (Read, capture, Bart's check at the scale. notebook "November 2")

## 8. The record

20 text fields separated by TAB. The scale returns each as it was sent. (Reply, read back)

The middle column compares a full send with ScaleLink's item table. "All 484" means every sent item matched that column. Where a field is constant, many columns match and we can't tell them apart. (`research/lab/correlate.py`)

| Pos | Meaning | Evidence | Confidence |
|---|---|---|---|
| 1 | Id | All 484 | Bart |
| 2 | Code | All 484 | Bart |
| 3 | PLU. What staff enter | All 484 | Bart |
| 4 | Description. ScaleLink drops trailing spaces | All 484 apart from those spaces | Bart |
| 5 | Price as text | All 484 | Bart |
| 6 | Image file name. Empty or one space on every real item | All 484, including which ones have the space | Inferred. Keep what the item has |
| 7 | Type. 1 by weight, 0 by piece | All 484, 19 of them 0. ScaleLink's product report prints 0 as not weighed | Inferred. Keep it |
| 8 | Probably price can be edited at the scale | Constant 1 | Inferred, weakly. Keep it |
| 9 | Probably shelf life in days | Constant 0. Follows the column order | Inferred, weakly. Keep it |
| 10 | Tax | All 484, including the one non zero value | Inferred, strongly. Keep it |
| 11 | Probably a multiple | Constant 0. Follows the column order | Inferred, weakly. Keep it |
| 12 | Unknown | Constant 0 | Keep it |
| 13 | Time of last edit, `YYYY-MM-DD HH:MM:SS` | All 484, reordered from the table | Bart for the format |
| 14 to 20 | Unknown. Probably some of ingredient, nutrition, ads, offer, tare | Constant 0 | Keep them |

For a new item, use what `DESIGN.md` says. Nobody has tested other values in fields 8, 9, 11, 12 or 14 to 20.

## 9. Limits

| Thing | What we know | Confidence |
|---|---|---|
| Ids | 1 to 500. See section 6 | Reply, read back |
| Items | 500 at most, since ids stop there | Inferred |
| Description, stored | 100 characters stored in full. Above that not tested | Reply, read back |
| Description, shown | Unknown. Longest real one is 35. Keep the limit at 35 until Bart checks a label | Unknown |
| Characters, stored | Raw single bytes stored as sent | Reply, read back |
| Characters, shown | Unknown. Real descriptions use only letters, digits, spaces and `( ) . - / # +`. Keep to printable ASCII until Bart checks | Unknown |
| Price | Scale stores anything. ScaleLink's message file gives 999.99 as the top for two decimals | Reply, read back |
| PLU and Code length | 3 and 5 digits stored. Whether they can be looked up is unknown. ScaleLink's message file says Code up to 999999 and PLU up to 99999 | Reply, read back |
| Records per batch | 5 proven by read back. 6, 10, 15, 18 got `Ok`, with no read back since the items were overwritten later | Reply |
| Two batches in one session | Proven: ScaleLink sends 97 | Reply, read back |

## 10. Timing and stalls

Reply waits on full sends in test9 and test10. (Reply. `research/lab/cap_timing.py`, `cap_stalls.py`, `cap_stall_period.py`)

| Message | Usual wait |
|---|---|
| `CX` | Under 0.01 s |
| `LP`, `Ok` in a read | 0.085 s |
| `bX01XX` | 1.04 s |
| `GP01` | 0.26 s |
| `GP05` | 0.71 s |
| `GPF0` | Under 0.01 s |

**Long waits.** On a write, about every 5.6 seconds one batch takes much longer. About 1.85 s on one scale, 1.75 s on another, and 3.25 to 3.29 s on the third. It falls on a time beat, not on a batch number. It looks like a repeating task inside the scale. (Inferred.) It did not appear during reads. ScaleLink's 3 second limit fails on the slowest scale because of this. Use a reply wait well above that. `DESIGN.md` says 15 s, which is fine.

**After `GPF0`.** Expect refused or unanswered connections for about 10 to 11 s after a session that changed anything. Retry a connection instead of failing. So verifying a write costs that wait plus a full read.

**Pace.** ScaleLink sends each batch about 10 to 40 ms after the reply and that works for 484 items on two scales. (Reply, read back on one of them.) The lab never went faster than one send a second. Never send the next message before the last reply.

**Connections at once.** Not tested. Lab limits kept to one at a time. By accident, a new session ran while the scale was still working on one I had half closed, and it seemed to queue. (Reply. p1 capture streams 61 and 62, 66 and 67.) Use one session per scale at a time.

**Full send time.** 484 items take about 90 s. (Reply. test9)

## 11. What the driver can do now

- Read with `LP`, compare every field as exact text, and use that for `verified`. The only pace tested is one send a second, about 100 s for a full table. Faster needs a test on the test scale.
- Keep `RI`, `bX01XX`, `GPF0` around writes, as ScaleLink does.
- Check each batch reply for `Ok` or `Error`. Treat anything else as `unknown`.
- Refuse bad values. The scale won't.
- Make sure the count in `GP` matches the records.
- Delete by overwriting the id with a placeholder. See section 7. Never send an `E` message.
- Change a PLU by writing the same id with a new Code and PLU. Code always follows the PLU.
- Say the scale may have frozen when a read gets no reply, or when it answered badly and then did not close when we hung up. See section 3.
- Read once more, once, only if that setting is on, and only after blocks out of order that came after a good block, on a scale that then closed. Never after any other bad answer, never after silence, and never on a write.
- Reads of production scales need Bart's go ahead at the final gate.

## 12. Still unknown

1. Whether `EP` deletes, its exact form, and whether it caused the outage. The library does not need to know, since it deletes by placeholder.
2. What a read returns when ids have gaps, and how a short last block looks.
3. Whether `GP` works without `RI`, `bX01XX` or `GPF0`, and what each one is for.
4. What `S` and `D` do.
5. Display and label limits for descriptions and characters.
6. What the display does with a duplicate PLU, a 3 or 5 digit PLU, a price over 999.99, and id 0.
7. Meaning of fields 8, 9, 11, 12 and 14 to 20.
8. Connections at once, and sending faster than ScaleLink does.
9. Batches above 5 with a read back.
10. Whether a read is safe at reply speed. The lab never went above one send a second.
11. Whether the three scales run the same firmware. So far only one scale has been read.
12. What FRONT sent in the read that stopped on November 2, and why. See section 3.

## 13. What the read changed

Earlier beliefs that turned out wrong or incomplete. (Reply, read back. notebook "5:53 PM", "Capture study".)

- The test scale did not hold ScaleLink's list and little else. It held 500 records, because test items from earlier sessions were still there. A full send had not touched them.
- Purge followed by a full send does not remove an item. The purged test item was still stored at id 485 afterward.
- Field 6 is not a mystery flag. It is very likely an image file name.
- Id 0 got `Ok`, yet is not in the read. So `Ok` to a record does not always mean it can be read back.
- `Ok` on `GP` for odd values is true in the narrow sense: the values were stored. It says nothing about whether they work on the display.
- `RI` may do nothing. It behaves like an unknown message.
- Reads exist, and they are quick, in the two full reads the lab ran. A capture with no read in it was not proof there is none.

## Label settings, seen only

ScaleLink sets the scale's label and barcode settings with their own messages. All were sent after `bX01XX`, and all were seen from captures only. They carry no product records, so a label change doesn't touch items. ScaleConnect doesn't send these and has no reason to. Bart keeps doing this in ScaleLink.

| Message | Seen doing |
|---|---|
| `Gi` | Barcode format |
| `GG` | Header text and image paths |
| `Gg` | Image paths |
| `GT` | Label words, including the bottom line |
| `GH` | Label top line |
| `FXX` plus the serial | Sent when a scale is added to ScaleLink. Reply `F0`. Meaning unknown |

ScaleLink's full product send doesn't go in id order.
