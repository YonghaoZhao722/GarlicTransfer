# macOS / PS5 14.22 transfer report

## Scope and evidence

One Monster Hunter Wilds (PPSA07862) migration was tested with an unmodified PS5
on firmware 14.22 and a macOS arm64 host (Node 22.22.2, Python 3.14.6).
The tested GarlicTransfer revision was `00f6f7b`.

The test used local Node wrappers calling the repository's Python backends and
`electron/remote_oracle.cjs`, with a volunteer HTOS oracle. The Electron app was
installed and launched, but the completed workflow was driven by the wrappers;
this is not a claim that a packaged DMG or every GUI control was verified.

Verified locally:

- DDP discovery returned the expected console and firmware.
- Browsing with the destination owner's decimal PSN account ID downloaded a
  valid metadata database. A physical PS5 power-button confirmation was needed.
- The selected user's Wilds entry downloaded two complete save containers.
  Sizes and SHA-256 digests matched the download manifest.
- After modifying only the character container through a separate game-format
  conversion/encryption workflow, the system container was byte-identical to
  its original backup.
- `poc.dt_metadata` produced metadata/manifest restricted to the selected user
  and title. Both upload payloads were sent in full, followed by successful
  `0xB3` and `0x94` responses. The destination rebooted.

The owner reported that the original Steam character was visible in the game.
Entering that character, saving/reloading, multiplayer and online identity
regeneration remained unconfirmed at the time of this report. This does not
establish compatibility for other game versions, DLC combinations or firmware.

## Workflow notes

1. Back up the Steam files and download the PS5 original save containers and
   metadata before making changes. Create an initial PS5 game save if needed.
2. Keep account IDs as strings. A decimal PSN account ID is not the online ID
   displayed in the PS5 profile. Do not substitute a rounded JavaScript Number.
3. Use a separate tool to decrypt the **outer PS5 container**, convert the inner
   game file, then re-encrypt for the destination account. GarlicTransfer does
   not convert a Steam game file by itself.
4. For this attempt, keep the destination's system container unchanged and
   replace only the character container. Keep an untouched backup directory
   separate from the upload assets.
5. Before upload, decrypt the generated container again and compare the inner
   game file byte-for-byte against the intended output. Verify the PSN account
   and title, and inspect that filtered metadata contains only the intended user
   and game.
6. Once verified, follow the upload procedure in the README, including removing
   the selected original destination save only after its backup is complete.
7. During **upload**, the destination's prompt refers to the source power
   button. The source is GarlicTransfer, which emits this signal automatically
   with `DT_AUTO_POWER=1`. Do not press the destination PS5 power button.
8. Verify the game can load, save and reload the character before calling the
   migration fully validated.

## Disconnect encountered during testing

Pressing the destination PS5 power button during the source-confirmation prompt
put it into rest mode. The connection ended before any save payload was sent.
The data listener then raised `EOFError: connection closed after 0/24 bytes`
and exited its thread while the command/discovery services remained alive.
Restarting the source backend allowed the subsequent transfer to complete.

This is distinct from **download** confirmation, where the physical PS5 button
was required in this test. Do not repeatedly press the destination power button
when an upload is waiting for the simulated source.

No raw logs, save contents, account IDs, console identifiers, network addresses
or credentials are included in this report.
