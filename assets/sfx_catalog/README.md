# Kids-book SFX catalog

This folder is a **fixed** list of picture-book and children's-adventure sounds (100+ slots). The story API does not search FreeSound while catalog mode is on (the default). It matches each cue to one entry in `catalog.json` and downloads that entry's preview.

Slots are grouped by category: `animals`, `weather`, `water`, `forest`, `home`, `magic`, `transport`, `city`, `story`, `time`, `action`, `creatures`, and `play`. Action slots stay cartoon-safe: a soft sword clash, a shield block, a punch whoosh, running footsteps, a tumble. Whooshes are specific: spell, punch, swing, arrow, and slide. Night pages use crickets, an owl, or wind.

`catalog.json` ships with the slots filled in except for FreeSound. `freesound_id` and `preview_url` stay empty until you run the builder once and listen to the results. The builder does not invent ids.

## Regenerate

From the repo root, with `FREESOUND_API_KEY` in `.env`:

```bash
python scripts/build_sfx_catalog.py
```

The script searches FreeSound **once per empty or rejected slot**, keeps a single sound (rating, downloads, CC0 then Attribution, duration), and writes it back into `catalog.json` with `"status": "pending"`. It also downloads preview MP3s into `previews/`.

Useful flags:

```bash
python scripts/build_sfx_catalog.py --refresh      # re-pick pending slots; approved slots stay
python scripts/build_sfx_catalog.py --no-download  # update JSON only
```

Approved rows are never replaced. A pending row that already has a FreeSound id is kept unless you pass `--refresh`. Rejected and empty rows get a new candidate. Preview files that are already on disk are not downloaded again.

The API token is sent only to `freesound.org`. Preview downloads do not send it.

## Listen and edit

1. Play the files in `assets/sfx_catalog/previews/` (`dog-bark.mp3`, `rain.mp3`, …). That directory is gitignored.
2. Open `catalog.json`.
3. For a sound you want to keep, set `"status": "approved"`.
4. For a sound you do not want, set `"status": "rejected"`. The next builder run fills that slot with a different FreeSound id.
5. To force a specific sound, paste its `freesound_id`, `preview_url` (the `preview-hq-mp3` URL), `license`, and `freesound_url`, then set `"status": "approved"`.

`download_url` points at FreeSound's original-file endpoint. That call needs OAuth, which this service does not use. The mixer plays `preview_url`.

Story requests skip a rejected row. They skip a row with no `preview_url` and log that this script still needs to run. Pending rows that already have a `preview_url` are used, so you can try the pack before every slot is marked approved.
