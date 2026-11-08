# ADR 0020: Seeding downloads are copied, never released

- **Status:** Accepted
- **Date:** 2026-11-06

## Context
Torrent support ([#301](https://github.com/marameowra/mixsync/issues/301)) adds a download client, qBittorrent, whose files must stay in place while they seed. Breaking that hurts ratio on private trackers and corrupts the torrent's data. Today every import ends with a **release**: a journaled hard delete of the source after the library copy is hash-verified ([data safety](../design/data-safety.md) §1). Dropped candidates are copied to quarantine and then released as well. Both would delete a seeding torrent's files. MixSync also writes tags to library files, which changes their bytes.

## Decision
- A torrent's files are a **seeding download**. MixSync copies from them (hash-verified, journaled) into the library or quarantine, and never moves, deletes or releases them.
- Removing a completed torrent and its files is left to qBittorrent's own ratio and seed-time rules. MixSync never calls remove on a completed torrent.
- A torrent dropped before it completes is removed from qBittorrent together with its partial files. Those files were never user music.
- MixSync adds torrents under its own category and save path, which the adopt and drop-folder scans never read.
- Soulseek and Usenet downloads keep the existing release rule.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Copy, leave the payload to the client (chosen)** | Seeding and the library never share bytes; no new delete path | Each album takes up disk twice until the client removes the torrent |
| Hard-link into the library | No extra disk | Tag writes change the seeding file and break the torrent; needs one filesystem |
| Copy, then release once seeding finishes | Frees disk sooner | MixSync would have to track ratio rules and delete files the client still owns |

## Consequences
- Disk use for torrent imports doubles until the client removes the torrent. Operators size seeding rules with that in mind.
- The journal records copies from the torrent save path but no release ops for them.
- Allowing hard links later would mean changing how tags are written, not just this rule.
