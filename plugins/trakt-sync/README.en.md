# Trakt playback tracking (beta)

[中文](README.md)

Sync movie and episode playback reported by MovieClaw to each connected member's own Trakt account.

> Not released in the market yet. Before publication, MovieClaw maintainers must configure an organization-maintained Trakt application and verify real refresh/revoke without a Client Secret. The default Client ID is intentionally empty. Ordinary users should wait for that setup; do not reuse the author's or someone else's credentials. See [application acceptance](APPLICATION.md).

## Install and update

Once listed, open **Settings → Plugins → Plugin Market**, find **Trakt 自动追踪**, review network/playback event permissions, and approve installation. Updates use the same market screen and preserve storage for the same plugin ID; no server restart is needed.

Open **Trakt** in the web sidebar, select the member, and follow device authorization to connect that member's Trakt account. Administrator ID is `0`; advanced settings accept another member ID. Unbound members never use the administrator's token. Management is administrator-only.

**Disconnect a member's Trakt account before deleting the MovieClaw member.** Member IDs can be reused, and this plugin currently has no member-deletion notification to clean up automatically.

Free Trakt accounts currently allow two Community Apps, excluding official Trakt apps. This plugin uses a slot; check [current policy](https://github.com/trakt/trakt-api/discussions/943) before revoking existing authorizations, since reauthorization can be restricted.

## Behavior and limitations

- Report online starts/resumes and stops/completions under Trakt's scrobble rules.
- Match TMDB/IMDb and season/episode IDs, never title guesses; keep each member's encrypted credentials and queue separate.
- Show bounded delivery details and retry matching failures. Start/progress heartbeats do not fill the activity list.
- Only clients reporting playback to MovieClaw are tracked. Infuse playing independently through Plex bypasses this plugin.
- No manual watched/unwatched, favorite, rating, bulk history or reverse-progress sync.

Automated manifest, contract, import and mocked-request checks use MovieClaw v0.40.0; online playback was previously tested with v0.41.0. These checks are not real-device acceptance. No running server changes are included.

Offline support is limited to events the host actually emits. Missing rewatches or historical completion events cannot be reconstructed. This version does not promise a usable `viewing_id` or complete per-offline-viewing history. The app must run again and reconnect; a full iPhone airplane-mode/playback/reconnection test remains pending.

Network failures retry with backoff and Retry-After. Each member's pending queue is bounded to 64 items/60 KB. Full queues first discard expired realtime jobs and heartbeats; if still full, older jobs are dropped with an explicit unsynced-count warning instead of blocking other members. Authorization failure stops new tasks, clears old pending tasks, and asks for disconnect/reconnect; those watches are not backfilled. Matching failures are kept in a bounded retry list.

Disconnect clears local credentials and pending work and attempts remote revocation. If unconfirmed, manage the grant in Trakt's Connected Apps. It never deletes watch history. Activity is a recent display, not a permanent archive or unlimited offline queue; exactly-once delivery is not guaranteed.

[Technical notes](docs/technical-notes.en.md) · [Privacy](PRIVACY.md) · [Changelog](CHANGELOG.md) · [Source](SOURCE.md)

MovieClaw Trakt uses Trakt but is not endorsed or certified by Trakt.
