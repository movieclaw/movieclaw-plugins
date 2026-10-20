"""Offline, allowlisted response-shape report; never reads account configuration.

Usage: python -m cloud_123_personal.acceptance < response.json
Only feed the response BODY of file/s3_list_upload_parts, never a HAR/request dump.
This does not validate progress or enable a production parser.
"""
import json
import sys


def summarize(value):
    report={"tool":"personal-upload-parts-shape-v1","network_used":False,"progress_verified":False}
    if not isinstance(value,dict):return {**report,"envelope":"not_object"}
    report['success_code']=type(value.get('code')) is int and value['code'] in (0,200)
    data=value.get('data')
    if not isinstance(data,dict):return {**report,'data_type':type(data).__name__}
    # Do not emit arbitrary keys or values: even field names can contain identifiers.
    for key in ('Parts','parts','IsTruncated','NextPartNumberMarker','NextMarker','next'):
        if key not in data:continue
        item=data[key]
        entry={'type':type(item).__name__}
        if isinstance(item,list):
            entry['count']=len(item)
            entry['all_objects']=all(isinstance(x,dict) for x in item)
            entry['fields_present']={field:sum(isinstance(x,dict) and field in x for x in item)
                                     for field in ('PartNumber','partNumber','Size','size','ETag','Etag','etag')}
        report[key]=entry
    return report


def main():
    raw=sys.stdin.buffer.read(1024*1024+1)
    if len(raw)>1024*1024:
        print('Response exceeds 1 MiB; nothing emitted.',file=sys.stderr);return 2
    try:value=json.loads(raw)
    except (ValueError,UnicodeError):
        print('Invalid JSON; no input content emitted.',file=sys.stderr);return 2
    print(json.dumps(summarize(value),ensure_ascii=True,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
