"""Audio file extensions: one list for migration, import and the Soulseek source."""

# Taken: tagged in place, except WAV/AIFF, which import encodes to FLAC (if re-encoding is off
# or the encode fails they import as they are, with ID3 tags).
EXTENSIONS = frozenset({".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".aiff", ".aif"})
# Audio mixsync cannot take: migration lists it as skipped instead of dropping it silently.
# Not sniffed by content: an extension in neither set is not treated as audio.
UNSUPPORTED = frozenset({
    ".aac", ".wma", ".ape", ".wv", ".alac", ".dsf", ".mka", ".m4b", ".mp4", ".oga", ".mpc",
    ".tta", ".dff", ".mp2", ".ac3", ".dts", ".ra", ".amr", ".au", ".caf", ".spx",
})  # fmt: skip
