# StatusTag
A compact glyph + word label for a state: match band, plan status, track status.

## Tones
| Class | Glyph | Means | Band / status |
|---|---|---|---|
| `confident` | ● | safe to proceed | `auto_accept`, done, verified |
| `review` | ◐ | uncertain, a person decides | `review`, unverified |
| `reject` | ✕ | will not proceed / failed | `reject`, failed |
| `neutral` | ○ | waiting or informational | pending, queued, in discover |

Colours: `positive`/`caution`/`danger` text on their `-tint`; neutral uses `ink-muted` on `surface-sunken`. The glyph comes from CSS; the word is the content, so meaning never depends on colour.

## Words
Map internal values to plain words: `auto_accept` → "Confident", `review` → "Needs review", `reject` → "Rejected". Keep the raw value in a `title` only if it helps debugging.

## Don't
Don't use a tag as a button or link. Don't invent tones; a fifth state uses `neutral`.
