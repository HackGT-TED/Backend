# Kids-book SFX catalog

This folder is a **fixed** list of picture-book and bedtime-story sounds. The story API does not search FreeSound while catalog mode is on (the default). It matches each cue to one entry in `catalog.json` and downloads that entry's preview.

`catalog.json` ships with the slots (dog bark, rain, a creaking door, and so on). `freesound_id` and `preview_url` stay empty until you run the builder once and listen to the results.

When `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set, the API reads the active `public.sfx_catalog` row (`id = 'active'`, `payload` jsonb) instead of this file. This file is the fallback for local and dev, and `SFX_CATALOG_PATH` forces a file. `python scripts/build_sfx_catalog.py --push` writes the file and stores that JSON in Supabase. The frontend marketplace is a different database.

## Regenerate

From the repo root, with `FREESOUND_API_KEY` in `.env`:

```bash
python scripts/build_sfx_catalog.py
```

The script searches FreeSound **once per empty or rejected slot**, keeps a single sound (rating, downloads, CC0 then Attribution, duration), and writes it back into `catalog.json` with `"status": "pending"`. It also downloads preview MP3s into `previews/`.

It then measures every non-rejected preview and writes `gain_db` on that entry, plus `loudness_target_dbfs` on the document. That gain is what the mixer applies so the clips share one volume. FreeSound's file is left as published.

```bash
python scripts/build_sfx_catalog.py --push
```

`--push` writes the file and upserts it to `public.sfx_catalog`. Run that after the gains look right so production uses the same JSON. A FreeSound API key is required only when a slot still needs a search. Measuring previews and pushing do not.

Useful flags:

```bash
python scripts/build_sfx_catalog.py --refresh      # re-pick pending slots; approved slots stay
python scripts/build_sfx_catalog.py --no-download  # measure gain_db without writing preview MP3s
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
