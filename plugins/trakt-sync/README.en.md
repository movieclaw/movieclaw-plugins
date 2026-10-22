# Trakt playback tracking

[中文](README.md)

Sync movie and episode playback reported by MovieClaw to each connected member's own Trakt account.

> The default public Trakt application is maintained by MovieClaw. No Client ID registration or Client Secret is required: sign in to your own Trakt account and approve the device authorization.

## Install and update

Open **Settings → Plugins → Plugin Market**, find **Trakt 自动追踪**, review network/playback event permissions, and approve installation. Updates use the same market screen and preserve storage for the same plugin ID; no server restart is needed.

Open **Trakt** in the web sidebar, select the member, and follow device authorization to connect that member's Trakt account. Administrator ID is `0`; advanced settings accept another member ID. Unbound members never use the administrator's token. Management is administrator-only.

**Disconnect a member's Trakt account before deleting the MovieClaw member.** Member IDs can be reused, and this plugin currently has no member-deletion notification to clean up automatically.

Free Trakt accounts currently allow two Community Apps, excluding official Trakt apps. This plugin uses a slot; check [current policy](https://github.com/trakt/trakt-api/discussions/943) before revoking existing authorizations, since reauthorization can be restricted.

## Behavior and limitations

- Report online starts/resumes and stops/completions under Trakt's scrobble rules.
- Match TMDB/IMDb and season/episode IDs, never title guesses; keep each member's encrypted credentials and queue separate.
- Show bounded delivery details and retry matching failures. Start/progress heartbeats do not fill the activity list.
- Only clients reporting playback to MovieClaw are tracked. Infuse playing independently through Plex bypasses this plugin.
- No manual watched/unwatched, favorite, rating, bulk history or reverse-progress sync.

Contracts, entrypoint, isolated host integration and automated regressions are validated against MovieClaw v0.42.0.

Offline support is limited to events the host actually emits. Missing rewatches or historical completion events cannot be reconstructed. This version does not promise a usable `viewing_id` or complete per-offline-viewing history. The app must run again and reconnect; a full iPhone airplane-mode/playback/reconnection test remains pending.

Network failures retry with backoff and Retry-After. Each member's pending queue is bounded to 64 items/60 KB. Full queues first discard expired realtime jobs and heartbeats; if still full, older jobs are dropped with an explicit unsynced-count warning instead of blocking other members. Authorization failure stops new tasks, clears old pending tasks, and asks for disconnect/reconnect; those watches are not backfilled. Invalid item requests (400) and matching failures (404/422) are kept in a bounded retry list without blocking the account. Loss warnings can be acknowledged without clearing cumulative totals or resending discarded watches; they do not permanently degrade plugin health.

Switching from the old personal application requires disconnecting the old binding and authorizing the new app. Revocation can take time to propagate; local disconnect stops delivery immediately.

Disconnect clears local credentials and pending work and attempts remote revocation. If unconfirmed, manage the grant in Trakt's Connected Apps. It never deletes watch history. Activity is a recent display, not a permanent archive or unlimited offline queue; exactly-once delivery is not guaranteed.

[Technical notes](docs/technical-notes.en.md) · [Privacy](PRIVACY.md) · [Changelog](CHANGELOG.md) · [Source](SOURCE.md)

MovieClaw Trakt uses Trakt but is not endorsed or certified by Trakt.

## Release tracks and recovery

`main` and the marketplace use stable 0.2.2. The separate `dev` / `v0.3.0-beta.1` track depends on unmerged host APIs and is not a production update.

Retries read current media metadata via `library.list` and `library.items.get`, using the original file ID when available. Completed views are checked against nearby Trakt history before adding a missing view with its original timestamp. Deleted media/file records retire the task; temporarily missing files wait. No media file access or writes are granted. Failed jobs retry with 10-minute to 6-hour backoff. Expired live events are retired, never converted into watched history.

Legacy records without local file IDs cannot be mapped automatically: correct their season/episode in the details or dismiss the record. Dismissal never removes Trakt history. Failed counts represent retained records, not a lifetime total.

To retain an existing application binding across this default-app change, explicitly save the previous public Client ID in advanced settings before upgrading. Reauthorization is required only when choosing to switch applications.
