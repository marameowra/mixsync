# AudioPreview
A list of the source files, each with a native audio player, so the reviewer can listen before deciding.

## Use
Position, file name, length (mono, `ink-muted`), then `<audio controls preload="none">` labelled with the file name. On review detail it sits beside the candidate tracklist from 48rem up and after the evidence on phones.

## The consumer provides
`/media/preview/<id>/<n>` sources. Never autoplay; `preload="none"` keeps the LAN quiet.
