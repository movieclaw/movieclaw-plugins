"""Read-only polling of MovieClaw's drive.upload job results."""
import asyncio
import json
import re
from datetime import datetime, timezone

class UploadMonitor:
    def __init__(self, sender):
        self.sender = sender
        self.started = datetime.now(timezone.utc).isoformat()

    async def successful_names(self, job_id, uploaded):
        after, pending, matched, names = 0, None, 0, []
        display_full = False
        for _ in range(50):
            response = await self.sender.enricher.call('jobs.events', {
                'job_id': job_id, 'after': after, 'limit': 500})
            if not isinstance(response, dict) or not isinstance(response.get('items'), list):
                return None
            events = response['items']
            for event in events:
                if type(event.get('id')) is not int or event['id'] <= after:
                    continue
                after = event['id']
                if event.get('event_type') != 'progress':
                    pending = None
                    continue
                progress = event.get('payload') or {}
                details = progress.get('details') or {}
                done, failed = details.get('done'), details.get('failed')
                if progress.get('phase') != 'uploading' or type(done) is not int or type(failed) is not int:
                    pending = None
                    continue
                match = re.fullmatch(r'(?:读取「(.+)」算指纹|上传「(.+)」)', progress.get('message') or '')
                if match:
                    pending = (match.group(1) or match.group(2), done, failed)
                elif pending and (done != pending[1] or failed != pending[2]):
                    if done == pending[1] + 1 and failed == pending[2]:
                        matched += 1
                        if len(names) < 20 and not display_full:
                            name = re.sub(r'[\x00-\x1f\x7f]', ' ', pending[0])
                            name = name[:300] + ('…' if len(name) > 300 else '')
                            if sum(len(v.encode()) + 5 for v in names) + len(name.encode()) + 5 <= 1200:
                                names.append(name)
                            else:
                                display_full = True
                    pending = None
            if matched >= uploaded or len(events) < 500:
                break
        return {'filenames': names, 'filenames_missing': max(0, uploaded - matched),
                'filenames_remaining': max(0, matched - len(names))}

    async def poll(self):
        sender = self.sender
        if not sender.cfg.configured() or not sender.cfg.uploads:
            return
        since = await sender.store.get('upload-monitor-since-v1')
        if not since:
            since = self.started
            await sender.store.set('upload-monitor-since-v1', since)
        response = await sender.enricher.call('jobs.list', {
            'job_type': 'drive.upload', 'status': 'succeeded', 'limit': 200})
        if not isinstance(response, dict) or not isinstance(response.get('items'), list):
            return
        cutoff = datetime.fromisoformat(since)
        seen = await sender.store.get('upload-notified-v1', default=[])
        seen = [v for v in seen if isinstance(v, str) and len(v) <= 128][-400:]
        for job in reversed(response['items']):
            if job.get('job_type') != 'drive.upload' or job.get('status') != 'succeeded':
                continue
            result = job.get('result') or {}
            uploaded, rapid, failed = (result.get(k) for k in ('uploaded', 'rapid', 'failed'))
            if not all(type(v) is int and v >= 0 for v in (uploaded, rapid, failed)) or not uploaded:
                continue
            try:
                finished = datetime.fromisoformat(str(job.get('finished_at')).replace('Z', '+00:00'))
                # Host database timestamps without offsets are UTC.
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=timezone.utc)
                if finished < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            job_id = job.get('id')
            if not isinstance(job_id, str) or not job_id or len(job_id) > 128 or job_id in seen:
                continue
            file_info = await self.successful_names(job_id, uploaded)
            if file_info is None:
                continue
            await sender.handle('drive.upload.completed', {
                **file_info,
                'title': job.get('subject') or '网盘上传', 'uploaded': uploaded,
                'rapid': rapid, 'failed': failed, '_occurred_at': finished.isoformat(),
            }, external_id='drive-upload:' + job_id)
            seen = (seen + [job_id])[-400:]
            while len(json.dumps(seen).encode()) > 32 * 1024:
                seen.pop(0)
            await sender.store.set('upload-notified-v1', seen)

    async def run(self):
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.sender.ctx.logger.warning('上传通知检查失败（%s），下次重试', type(exc).__name__)
            await asyncio.sleep(self.sender.cfg.upload_poll_seconds)
