# Trakt playback tracking

[中文](README.md)

Sync movie and episode playback reported by MovieClaw to each connected member's own Trakt account.

> The default public Trakt application is maintained by MovieClaw. No Client ID registration or Client Secret is required: sign in to your own Trakt account and approve the device authorization.

> `0.3.0` requires official MovieClaw Server **v0.44.0 or a compatible later release**, including the playback event 1.1 contracts from [upstream #1063](https://github.com/movieclaw/MovieClaw/pull/1063). Servers v0.43.0 and earlier cannot install this version. Marketplace availability follows the upstream catalog's review and release process.

## Install and update

Open **Settings → Plugins → Plugin Market**, find **Trakt 自动追踪**, review network, playback/mark events, metadata-read and history-import permissions, and approve installation. Updates use the same market screen and preserve storage for the same plugin ID; no server restart is needed.

Open **Trakt** in the web sidebar, select the member, and follow device authorization to connect that member's Trakt account. Administrator ID is `0`; advanced settings accept another member ID. Unbound members never use the administrator's token. Management is administrator-only.

**Disconnect a member's Trakt account before deleting the MovieClaw member.** Member IDs can be reused, and this plugin currently has no member-deletion notification to clean up automatically.

Free Trakt accounts currently allow two Community Apps, excluding official Trakt apps. This plugin uses a slot; check [current policy](https://github.com/trakt/trakt-api/discussions/943) before revoking existing authorizations, since reauthorization can be restricted.

## Behavior and limitations

- Report online starts/resumes and stops/completions under Trakt's scrobble rules.
- Match TMDB/IMDb and season/episode IDs, never title guesses; keep each member's encrypted credentials and queue separate.
- Show bounded delivery details and retry matching failures. Start/progress heartbeats do not fill the activity list.
- Only clients reporting playback to MovieClaw are tracked. Infuse playing independently through Plex bypasses this plugin.
- Three independent, default-off member preferences: manual watched, manual unwatched, and new favorites to Trakt Watchlist. Unwatched removes all Trakt watches for that movie/episode, including rewatches. Unfavorite does not remove Watchlist entries; ratings are absent.
- Manual, foreground-only history export/import: scan, review full movie/per-season groups, Confirm, then batched progress. Hiding/closing the page stops new batches; resuming is explicit. Trakt stores UTC minutes: export floors the remote timestamp without changing local seconds/microseconds, and import preserves the actual API-returned time. Rewatches in different minutes remain distinct; extra same-minute source watches are disclosed and skipped. Match only local holdings.
- A separate History page loads batch summaries first and details on demand. Durable receipts and activity/inventory checks support incremental reconciliation; playback progress and media files are not imported.

Catalog/import operations shipped in #973. Sharing source receipts between real-time delivery and history export uses the common watch-log IDs from #1063. Paired tests pin official v0.44.0; installing the plugin needs no server source patch or additional port. See the [workflow](docs/sync-workflow.zh-CN.md) and [minute precision contract](docs/trakt-time-contract.zh-CN.md). Lost remote seconds cannot be reconstructed.

Offline support is limited to events the host actually emits. Missing rewatches or historical completion events cannot be reconstructed. This version does not promise a usable `viewing_id` or complete per-offline-viewing history. The app must run again and reconnect; a full iPhone airplane-mode/playback/reconnection test remains pending.

Network failures retry with backoff and Retry-After. Each member's pending queue is bounded to 64 items/60 KB. Full queues first discard expired realtime jobs and heartbeats; if still full, older jobs are dropped with an explicit unsynced-count warning instead of blocking other members. Authorization failure stops new tasks, clears old pending tasks, and asks for disconnect/reconnect; those watches are not backfilled. Invalid item requests (400) and matching failures (404/422) are kept in a bounded retry list without blocking the account. Loss warnings can be acknowledged without clearing cumulative totals or resending discarded watches; they do not permanently degrade plugin health.

Switching from the old personal application requires disconnecting the old binding and authorizing the new app. Revocation can take time to propagate; local disconnect stops delivery immediately.

Disconnect clears local credentials and pending work and attempts remote revocation. If unconfirmed, manage the grant in Trakt's Connected Apps. It never deletes watch history. Activity is a recent display, not a permanent archive or unlimited offline queue; exactly-once delivery is not guaranteed.

[Technical notes](docs/technical-notes.en.md) · [Privacy](PRIVACY.md) · [Changelog](CHANGELOG.md) · [Source](SOURCE.md)

MovieClaw Trakt uses Trakt but is not endorsed or certified by Trakt.

## Release tracks and recovery

Stable releases use `main` and matching `vX.Y.Z` Releases; `0.3.0` targets official Server v0.44.0. Development candidates retain beta labels on `dev`. Our GitHub Release and marketplace publication are separate steps; catalog updates follow upstream review and release procedures.

Retries read current media metadata via `library.list` and `library.items.get`, using the original file ID when available. Completed views are checked against nearby Trakt history before adding a missing view with its original timestamp. Deleted media/file records retire the task; temporarily missing files wait. No media file access or writes are granted. Failed jobs retry with 10-minute to 6-hour backoff. Expired live events are retired, never converted into watched history.

Legacy records without local file IDs cannot be mapped automatically: correct their season/episode in the details or dismiss the record. Dismissal never removes Trakt history. Failed counts represent retained records, not a lifetime total.

To retain an existing application binding across this default-app change, explicitly save the previous public Client ID in advanced settings before upgrading. Reauthorization is required only when choosing to switch applications.
