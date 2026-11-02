# Scale prices: staff guide

For anyone at the deli counter. Keep it short. Read the top part first.

## The most important rules

1. After a change, look at the scale. Check the price on the display.
2. If a scale shows a problem, do not try again and again. Tell Bart.
3. Do not send items from ScaleLink unless Bart says so.

## Open the page

1. Click the "Scale Prices" icon.
2. Type the PIN.
3. You can now find items and make changes.

On a phone, use the web address Bart gave you.

## Find an item

1. Tap the search box.
2. Type the PLU number, or part of the name.
3. Tap the item in the list.

If it says "Nothing found", type fewer letters. Check the PLU.

## Change a price

1. Find the item.
2. Tap "Change this item".
3. Type the new price. Example: 9.99
4. Tap "Review the change".
5. Read the screen. Check the old price and the new price.
6. Tap "Send to the scales".
7. Wait. It can take a few minutes. The send keeps going if your phone locks. Come back to this page to see how it went.
8. Read the result for each scale.

The price must be more than 0 and no more than 999.99.

If the new price is far from the old price, the page asks you to confirm. Look again. A typing slip is easy to miss.

## Add a new item

1. Tap "Add a new item".
2. Type the PLU. It is 4 digits.
3. Type the description.
4. Type the price.
5. Choose "By weight" or "By piece".
6. Tap "Review the new item".
7. Check it. Tap "Send to the scales".
8. Wait for the result.

The PLU cannot already be in use.

## Change a PLU

Think before you change a PLU. Staff look items up by PLU, and the old number stops working.

1. Find the item.
2. Tap "Change this item".
3. Type the new PLU in the PLU box. It is 4 digits.
4. Tap "Review the change".
5. Read the screen. It shows the old PLU and the new PLU. It says the old PLU stops working on the scales once this runs.
6. Tick the box that says you understand.
7. Tap "Send to the scales".
8. Wait for the result. Read the line for each scale.

The code on the item always follows the PLU. You do not set it.

The new PLU must be 4 digits and cannot already be used by another item. The numbers 9001 to 9500 are set aside and cannot be used.

If one scale did not take the change, the result says so and shows "Repeat that edit". Look at each scale first. Then tap "Repeat that edit" and check the screen. Until every scale says "changed and checked", do not give the old PLU or the new PLU to another item. If the screen says a scale is still on the old PLU and tells you to repeat that edit, open "Recent changes", open that change, and tap "Repeat that edit".

## Delete an item

1. Find the item.
2. Scroll down. Tap "Delete this item".
3. Read the screen. It shows the PLU, the name and the price of the item you are about to delete. Make sure it is the right one.
4. The screen says the PLU will stop working on the scales. Tick the box that says you understand.
5. Tap "Delete from the scales".
6. Wait for the result. Read the line for each scale.

If it is the wrong item, tap "Do not delete. Go back." Nothing has been sent yet.

After a delete, the old PLU does nothing on the scales. The page no longer finds the item. A scale that was not changed still has the old item until the result says "changed and checked" for it.

## What "UNUSED" means

A deleted item leaves an empty slot on each scale. The slot is named UNUSED. It has a PLU from 9001 to 9500 and a price of 0.01. If you ever see UNUSED on a scale or a label, it is an empty slot, not a real product. Do not sell it. Do not try to change it. Tell Bart if you see it on a label.

## What you cannot do here

- You cannot change a scale's settings, units or calibration.
- You cannot change a PLU to a number another item uses.
- If something looks wrong with a delete or a PLU change, ask Bart.

## What each result means

You see one line for each scale.

| You see | What it means | What to do |
|---|---|---|
| Changed and checked | The scale has the new item or price. It was read back and matched. | Nothing. |
| Accepted, but not checked | The scale said OK. It was not read back. | Look at the scale. |
| The scale refused it | The scale said Error. It should not have changed. | Look at the scale. Tell Bart. |
| No clear answer | We do not know if it changed. | Look at the scale. Do not send again. Tell Bart. |
| The scale took it, but now shows something different | The scale holds other values than you asked for. | Look at the scale. Tell Bart. |
| Could not connect | Nothing was sent. The scale is as it was. | Check the scale is on and on WiFi. Try again in a minute. |
| Could not read the scale first | Nothing was sent. | Try again in a minute. |
| This is not the scale it should be | Nothing was sent. | Tell Bart. |
| The scale holds something different from our list | Nothing was sent. | Tell Bart. Do not force it. |
| Changes are turned off for this scale | Nothing was sent to this scale. | Tell Bart. That scale still has the old price. |
| This action is not available | Nothing was sent. | Ask Bart. |
| An earlier PLU change did not finish on this scale | Nothing was sent. The scale is still on the old PLU. | Repeat that earlier edit. See "Change a PLU". |
| This scale already has that PLU on a different item | Nothing was sent. | Tell Bart. Do not force it. |
| The scale did not answer. It may need a reboot. | The scale may have frozen. A reboot means turning it off and on again. | Tell Bart. See below. |

At the top you see one line for the whole change.

- "Done. Every scale was changed and checked." means all is well.
- "Only partly done" means some scales have the new price and some do not. Read each line.
- Anything else means look at the scales.

Never tell a customer a price is changed until every scale says "changed and checked", or you have looked at the scale yourself.

## If a scale shows a problem

1. Do not send the same change again and again.
2. Look at the scale display. Is the price right? Is the item there?
3. Look at the page. Open "Recent changes" to see the result.
4. If the page stopped or the phone went to sleep, open it again and look at "Recent changes".
5. Tell Bart what you saw. Say which scale.

If one scale says "could not connect", wait a minute. A scale can be busy for about 10 seconds after a change.

## If a scale may need a reboot

Sometimes the page says: "The scale did not answer. It may need a reboot. Tell Bart."

This means the scale stopped answering the page. It may have frozen. It can still weigh while it is frozen.

1. Do not keep trying. Do not send the change again and again.
2. Tell Bart which scale it was.
3. Bart decides whether to turn the scale off and on, and whether to reload its items.

Nothing was changed on a scale that did not answer, unless the line for that scale says otherwise. Check the price on the scale display.

For Bart: the reload command is in `docs/CLI.md`.

Never touch the scale's calibration or settings. ScaleConnect does not change them.

## What to avoid in ScaleLink

ScaleLink is Tor-rey's program on the Windows PC. It can undo our work.

- A send from ScaleLink can overwrite what ScaleConnect set. Old prices can come back.
- ScaleLink's purge does not remove an item from a scale. The item stays on the scale.
- Do not send from ScaleLink unless Bart says so.
- If someone must use ScaleLink, tell Bart first. The page will show a difference next time.

## Who to ask

Ask Bart.
