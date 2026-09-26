Drop local catalog clips in this folder before running the smoke test.

Name each file with the catalog id from assets/sfx_catalog/catalog.json:

  rain.mp3
  door-creak.wav
  bird-chirp.mp3
  dog-bark.mp3
  footsteps-wood.mp3
  wind.mp3
  thunder.mp3
  pages-turning.mp3
  magic-chime.mp3
  kettle-whistle.mp3
  owl-hoot.mp3
  yawn.mp3

mp3, wav, ogg, flac, and m4a are accepted. The id is the filename stem.
Audio files here are gitignored. This note stays in git.

The runner also checks assets/sfx_catalog/previews/ (from scripts/build_sfx_catalog.py)
if a clip is not in this folder. Set SFX_CATALOG_DIR to use a different directory.
