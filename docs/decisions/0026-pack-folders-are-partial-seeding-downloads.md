# ADR 0026: Pack folders are partial seeding downloads

- **Status:** Accepted
- **Date:** 2026-11-12

## Context
On many trackers, the only copy of an older album is inside a pack: one torrent spanning several album folders (a discography or label pack). The Prowlarr source skips packs today. #328 lets a request take one album folder from a pack. [ADR 0022](0022-seeding-downloads-copied-never-released.md) makes every torrent a seeding download that mixsync copies from and never deletes once it has finished. A partial download adds three questions: what the client downloads, what happens when a second request wants another folder of the same pack, and whether private trackers accept partial downloads.

## Decision
- Each album folder in a pack is scored as its own candidate, and a pack contributes only its best-matching folder to a request. Its Tried list entry names that folder, so the pack's other folders stay eligible.
- A pack is added to qBittorrent stopped. Every file is set to "do not download" except the chosen folder's files, and then the torrent is started. If selection fails, the torrent is removed before any data arrives.
- A shared pack's selection only grows. A later request's folder is added to the existing torrent, never replacing another request's folder. A folder is deselected only when no live request needs it, and only while the torrent hasn't finished.
- Once its selected files are complete, a pack folder is a seeding download under ADR 0022: copied, never released, and removed only by the client's own rules.
- The fragments libtorrent keeps at file boundaries (`.<infohash>.parts` in the save path, which no scan reads) are left to qBittorrent, which deletes them with the torrent.
- Pack folders are an admin opt-in, off by default, because some private trackers may count a partial download against hit-and-run or ratio rules.
- Torrents only: an NZB can't be partly downloaded.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Select one folder (chosen)** | Downloads and stores one album | Partial seeding; possible hit-and-run on some trackers |
| Download the whole pack, copy one folder | Seeds the full torrent | Disk space and bandwidth for a discography to get one album |
| Keep skipping packs | Nothing to change | Older albums found only in packs stay out of reach |
| **Opt-in, off by default (chosen)** | Respects tracker rules unless the admin accepts the risk | One more setting |
| Always on | Works out of the box | Can break private-tracker rules unnoticed |

## Consequences
- A partial torrent seeds only its selected folders, so ratio builds more slowly than for a full pack.
- When two requests share a pack, it depends on the shared-download fix (#452).
- The 5.2+ `filePriorities` add parameter is not used, because it rejects uploaded `.torrent` files.
