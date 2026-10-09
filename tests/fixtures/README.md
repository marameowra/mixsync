# `tests/fixtures`

Test data shared by the unit and integration tests.

## Layout
```
audio/                      short CC-licensed clips + manifest (see audio/README.md)
slskd/                      recorded slskd search responses (JSON), added with the slskd adapter
labeled/                    matcher regression cases: candidate/file + MB target + expected band
regression_baseline.json    stored precision/recall per band per profile
```
Only `audio/` exists now. Create the others when the first test needs them.

## Rules
- No copyrighted audio. Ever. Clips must be CC0/CC-BY/CC-BY-SA, or generated test tones.
- Recorded slskd responses are scrubbed of real peer usernames (replace them with `peer-001`, …).
- Each labeled case has one line explaining why its expected band is correct.
- Total fixture size stays under 50 MB. Larger corpora live outside the repo and are fetched by a script.

## Design docs
[Testing: matcher regression suite](../../docs/testing.md#matcher-regression-suite)
