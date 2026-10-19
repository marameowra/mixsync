# Artwork
A square cover image sized by context, with a calm fallback when Cover Art Archive has none.

## Sizes
| Class | Token | Context |
|---|---|---|
| `.art.xs` | `art-xs` | table rows (requests, watchlist, discover tracks) |
| `.art` | `art-sm` | review and plan headers: identifies without pushing evidence down |
| `.art.md` | `art-md` | watchlist follow; release detail on phones |
| `.art.lg` | `art-lg` | discovery and release detail from 48rem up |

## Fallback
`.art.missing`: `surface-sunken` square, hairline border, the release title's first letter in `ink-muted`, `role="img"` with `aria-label="No cover art for <title>"`. Never a stock image or a music-note emoji.

## The consumer provides
`<span class="art …"><img src="/media/cover/<id>" alt="Cover of <title>" width height loading="lazy"></span>`, with the fallback served when the route returns 404.
