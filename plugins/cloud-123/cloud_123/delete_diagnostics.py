"""删除诊断：只记受控结构，不记录凭据、远端 ID 或原始响应。"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import secrets


_current = ContextVar("cloud_123_delete_diagnostics", default=None)
_logger = logging.getLogger(__name__)
_request_fields = ("driveId", "driveid", "event", "operation", "operatePlace",
                   "fileTrashInfoList", "filetrashinfolist", "fileIdList")
_data_fields = ("infoList", "InfoList", "fileTrashInfoList", "fileIdList", "FileId")


def record(event, **fields):
    state = _current.get()
    if state is not None:
        try:
            _logger.info("123-delete-diag %s", json.dumps({
                "trace": state["trace"], "phase": state["phase"], "event": event, **fields,
            }, ensure_ascii=True))
        except Exception:
            # 诊断输出失败不得改变删除结果或覆盖原始异常。
            pass


@contextmanager
def trace(targets):
    state = {"trace": secrets.token_hex(6), "phase": "precheck",
             "files": {remote_id: index + 1 for index, remote_id in enumerate(targets)}}
    token = _current.set(state)
    try:
        record("start", count=len(targets))
        yield state
    except BaseException as error:
        record("failed", error_type=type(error).__name__)
        raise
    else:
        record("returned")
    finally:
        _current.reset(token)


def _file_summary(row, targets):
    if not isinstance(row, dict):
        return {"row_type": type(row).__name__}
    remote_id = row.get("FileId")
    numeric = (type(remote_id) in (int, str) and str(remote_id).isascii()
               and str(remote_id).isdecimal() and len(str(remote_id)) <= 20)
    file = targets.get(str(int(remote_id))) if numeric else None
    summary = {"file": file, "id_match": file is not None,
               "response_id_type": type(remote_id).__name__}
    for key in ("Type", "Trashed"):
        value = row.get(key)
        summary[key] = value if type(value) in (int, bool) and value in (0, 1) else None
        summary[key + "_type"] = type(value).__name__
    return summary


def observe(request, *, response=None, result=None, parsed=False):
    state = _current.get()
    if state is None or request.url.path not in ("/b/api/file/info", "/b/api/file/trash"):
        return
    try:
        payload = json.loads(request.content)
        rows = payload.get("fileIdList", payload.get("fileTrashInfoList", payload.get("filetrashinfolist", [])))
        files = [{"file": state["files"].get(str(row.get("FileId"))),
                  "id_type": type(row.get("FileId")).__name__} for row in rows]
        if response is None:
            # prepared 不等于已发送；宿主预算仍可在实际发送前拒绝。
            record("request_prepared", files=files,
                   fields=[key for key in _request_fields if key in payload],
                   other_fields=len(payload.keys() - set(_request_fields)))
        elif not parsed:
            record("http_response", files=files, status=response.status_code)
        else:
            code = result.get("code") if isinstance(result, dict) else None
            data = result.get("data") if isinstance(result, dict) else None
            fields = {"code": code if type(code) is int and 0 <= code <= 99999 else None,
                      "code_type": type(code).__name__, "result_type": type(result).__name__,
                      "data_type": type(data).__name__}
            if isinstance(data, dict):
                fields["data_fields"] = {key: type(data[key]).__name__ for key in _data_fields if key in data}
                fields["other_data_fields"] = len(data.keys() - set(_data_fields))
                info = data.get("infoList")
                if request.url.path.endswith("/info") and isinstance(info, list):
                    fields["rows"] = len(info)
                    if len(info) == 1 and isinstance(info[0], dict):
                        fields.update(_file_summary(info[0], {str(rows[0]["FileId"]): files[0]["file"]}))
                if request.url.path.endswith("/trash"):
                    info = data.get("InfoList")
                    fields["InfoList_present"] = "InfoList" in data
                    fields["InfoList_type"] = type(info).__name__
                    if isinstance(info, list):
                        targets = {str(row["FileId"]): file["file"] for row, file in zip(rows, files)}
                        fields["trash_rows"] = len(info)
                        fields["trash_items"] = [_file_summary(row, targets) for row in info]
            record("api_response", files=files, **fields)
    except Exception:
        record("observation_unavailable")
