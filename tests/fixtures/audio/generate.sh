#!/bin/sh
# Regenerates the cc0-tone-01.* clips (a 1 s 440 Hz sine). Run from this directory.
set -e
SRC="-f lavfi -i sine=frequency=440:duration=1:sample_rate=22050 -ac 1 -map_metadata -1 -bitexact -y -loglevel error"
ffmpeg $SRC cc0-tone-01.flac
ffmpeg $SRC -ac 2 -c:a vorbis -strict -2 cc0-tone-01.ogg
ffmpeg $SRC -c:a aac -b:a 32k cc0-tone-01.m4a
ffmpeg $SRC -c:a libmp3lame -b:a 32k -id3v2_version 0 -write_id3v1 0 cc0-tone-01.mp3
