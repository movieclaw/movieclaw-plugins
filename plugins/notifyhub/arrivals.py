"""Coalesce files-added deliveries without acknowledging before send succeeds."""
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone, timedelta


def date(value):
    try:
        when = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when
    except (ValueError, TypeError):
        return None


class FastArrivals:
    def __init__(self, sender):
        self.sender = sender
        self.pending = {}

    async def handle(self, payload):
        data = payload.model_dump(mode='json') if hasattr(payload, 'model_dump') else payload
        # Full scans and large batches retain the host's quieter, filtered fallback.
        if not self.sender.cfg.library_arrivals or len(data['files']) > 200:
            return
        library_id = data['library_id']
        files = [f for f in data['files'] if f.get('media_item_id') is not None]
        if not files:
            return
        batch = self.pending.get(library_id)
        if batch is None:
            batch = {'files': {}, 'started': time.monotonic(), 'updated': time.monotonic(),
                     'future': asyncio.get_running_loop().create_future()}
            self.pending[library_id] = batch
            self.sender.ctx.task(self.flush(library_id, batch), name=f'library-arrival-{library_id}')
        batch['files'].update({f['id']: f for f in files})
        batch['updated'] = time.monotonic()
        # Each host delivery remains unacknowledged until the coalesced send finishes.
        await asyncio.shield(batch['future'])

    async def flush(self, library_id, batch):
        future = batch['future']
        try:
            quiet = self.sender.cfg.library_merge_seconds
            while True:
                wait = min(quiet - (time.monotonic() - batch['updated']),
                           3 * quiet - (time.monotonic() - batch['started']))
                if wait <= 0:
                    break
                await asyncio.sleep(wait)
            self.pending.pop(library_id, None)
            await self.send(library_id, list(batch['files'].values()))
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
        else:
            if not future.done():
                future.set_result(None)
        finally:
            if self.pending.get(library_id) is batch:
                self.pending.pop(library_id, None)

    async def send(self, library_id, files):
        if len(files) > 200 or len({f['media_item_id'] for f in files}) > 20:
            return
        libraries = await self.sender.enricher.call('library.list', {})
        if not isinstance(libraries, list):
            # Keep the filtered native items-arrived event as a fallback.
            return
        library = next((v for v in libraries if v.get('id') == library_id), None)
        if not library or library.get('kind') not in ('movie', 'tv', 'other'):
            return
        grouped = {}
        for file in files:
            grouped.setdefault(file['media_item_id'], set()).add(file['id'])
        for item_id, ids in grouped.items():
            detail = await self.sender.enricher.call('library.items.get', {
                'library_id': library_id, 'media_item_id': item_id})
            if not isinstance(detail, dict) or not detail.get('title') or detail.get('source') == 'local':
                continue
            rows = detail.get('files') or []
            selected = []
            for row in rows:
                if row.get('id') not in ids or row.get('missing') or row.get('state', 'in_place') != 'in_place':
                    continue
                added, created = date(row.get('added_at')), date(library.get('created_at'))
                if row.get('source') == 'scanned' and (not added or not created or added - created < timedelta(hours=24)):
                    continue
                unit = (row.get('season_number'), row.get('episode_number'))
                if any(type(old.get('id')) is int and old['id'] < row['id'] and
                       (old.get('season_number'), old.get('episode_number')) == unit for old in rows):
                    continue  # An earlier version/rename of this movie or episode already existed.
                selected.append(row)
            if not selected:
                continue
            media = {'id': item_id, **{k: detail.get(k) for k in ('kind', 'title', 'year', 'tmdb_id', 'imdb_id')}}
            units = [{'season': f.get('season_number'), 'episode': f.get('episode_number')} for f in selected]
            event_key = hashlib.sha256(json.dumps([library_id, item_id, sorted(f['id'] for f in selected)]).encode()).hexdigest()
            await self.sender.handle('library.items.arrived', {
                'library_id': library_id, 'media': media, 'units': units,
                'files': [{'size_bytes': f.get('size_bytes')} for f in selected],
                '_occurred_at': max((str(f.get('added_at') or '') for f in selected), default=''),
            }, external_id='files-added:' + event_key)
