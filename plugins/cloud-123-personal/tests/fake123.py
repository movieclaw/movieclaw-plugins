"""In-memory protocol simulator. It is not a recorded or service-verified fixture."""
import asyncio
import hashlib
import json
import secrets
import httpx


class Fake123:
    def __init__(self):
        self.token = secrets.token_urlsafe(40)
        self.rows = {}
        self.calls = []
        self.parts = {}
        self.upload = None
        self.error = None
        self.put_error = None
        self.closed = False
        self.lose_put = self.lose_complete = self.lose_stat = False
        self.complete_parent = None
        self.complete_response = None
        self.cancel_complete = self.lose_init = False
        self.reuse_id = None

    def row(self, fid, name, size=0, parent=0, directory=False, content=b""):
        item = {"FileId": fid, "FileName": name, "Type": int(directory), "Size": size,
                "Etag": hashlib.md5(content).hexdigest(), "ParentFileId": parent,
                "S3KeyFlag": "synthetic-object", "Status": 0, "Expired": False}
        self.rows[str(fid)] = item
        return item

    async def handle(self, request):
        path = request.url.path.removeprefix("/b")
        payload = json.loads(request.content) if request.content and request.method != "PUT" else {}
        self.calls.append((path, payload, dict(request.headers)))
        if self.error:
            return httpx.Response(self.error[0], json=self.error[1])
        if request.method == "PUT":
            if self.put_error:
                return httpx.Response(self.put_error)
            self.parts[int(request.url.path.rsplit('/',1)[1])] = request.content
            if self.lose_put:
                self.lose_put=False
                raise httpx.ReadError("synthetic lost PUT acknowledgement")
            return httpx.Response(200)
        if path == "/api/user/sign_in":
            data = {"token": self.token}
        elif path == "/api/user/info":
            data = {"UID": 17, "Nickname": "测试用户", "SpacePermanent": 100, "SpaceUsed": 20}
        elif path == "/api/file/info":
            if self.lose_stat and '999' in self.rows:
                self.lose_stat=False
                raise httpx.ReadError("synthetic lost stat response")
            data = {"infoList": [self.rows[str(x['FileId'])] for x in payload['fileIdList'] if str(x['FileId']) in self.rows]}
        elif path in ("/api/file/list/new", "/api/file/list"):
            params = request.url.params
            parent = int(params['parentfileid'])
            rows = [r for r in self.rows.values() if r['ParentFileId'] == parent]
            page = int(params['page']);start=(page-1)*100
            data = {"InfoList": rows[start:start+100], "Next": "-1" if start+100>=len(rows) else str(rows[start+99]["FileId"])}
        elif path == "/api/file/upload_request":
            if payload['type'] == 1:
                fid = max([int(x) for x in self.rows] + [0]) + 1
                data = {"Info": self.row(fid,payload['filename'],parent=int(payload['parentfileid']),directory=True), "Reuse": False}
            else:
                if self.lose_init:
                    self.lose_init=False
                    self.row(999,payload['filename'],payload['size'],int(payload['parentfileid']))
                    self.rows['999']['Etag']=payload['etag']
                    raise httpx.ReadError("synthetic lost initialization response")
                if self.reuse_id is not None:
                    return httpx.Response(200,json={"code":0,"data":{"Reuse":True,"FileId":self.reuse_id}})
                self.upload = payload
                data = {"FileId": 999, "Reuse": False, "SliceSize": 4, "Bucket": "test-bucket", "Key": "test-object", "StorageNode": "test-node", "UploadId": "test-upload"}
        elif path in ("/api/file/s3_repare_upload_parts_batch", "/api/file/s3_upload_object/auth"):
            number = payload.get('partNumberStart',1)
            data = {"presignedUrls": {str(number): f"https://storage.example.test/part/{number}"}}
        elif path == "/api/file/upload_complete/v2":
            p = self.upload
            content = b''.join(self.parts[n] for n in sorted(self.parts))
            self.row(999,p['filename'],len(content),int(p['parentfileid']),content=content)
            if self.complete_parent is not None:
                self.rows['999']['ParentFileId']=self.complete_parent
            if self.cancel_complete:
                raise asyncio.CancelledError
            if self.lose_complete:
                self.lose_complete=False
                raise httpx.ReadError("synthetic lost completion acknowledgement")
            if self.complete_response is not None:
                return httpx.Response(200,json=self.complete_response)
            data = {}
        elif path == "/api/file/download_info":
            data = {"DownloadUrl": "https://download.example.test/original"}
        elif path == "/api/file/trash":
            for x in payload['filetrashinfolist']:
                self.rows.pop(str(x['FileId']),None)
            data = {}
        else:
            raise AssertionError(f"Unexpected endpoint: {path}")
        return httpx.Response(200,json={"code":0,"data":data})

    def transport(self):
        fake = self
        class Transport(httpx.MockTransport):
            async def aclose(self):
                fake.closed = True
        return Transport(self.handle)
