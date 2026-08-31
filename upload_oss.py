#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 open_data/ 推到阿里云 OSS。配置全部走环境变量，仓库里不出现任何密钥。

  open_data/full/   -> oss://$OSS_BUCKET/v1/full/          私有，需只读密钥才能取
  open_data/public/ -> oss://$OSS_BUCKET/p/$PREFIX/         公共读，但路径不可猜

用 oss2 SDK 而非 ossutil：官方二进制的下载地址会随版本失效，pip 包更稳，
而且能精确控制每个对象的 ACL。
"""
import os
import sys

try:
    import oss2
except ImportError:
    print("[错误] 缺少 oss2，请先 pip install oss2", file=sys.stderr)
    sys.exit(1)

ENDPOINT = os.environ.get("OSS_ENDPOINT", "").strip()
BUCKET = os.environ.get("OSS_BUCKET", "").strip()
KEY_ID = os.environ.get("OSS_KEY_ID", "").strip()
KEY_SECRET = os.environ.get("OSS_KEY_SECRET", "").strip()
PUBLIC_PREFIX = os.environ.get("OSS_PUBLIC_PREFIX", "").strip()

# 浏览器/pandas 直接取 URL 时，这些类型不该被当成附件下载
CONTENT_TYPE = {
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".gz": "application/gzip",
    ".parquet": "application/octet-stream",
}


def upload_dir(bucket, local_dir, remote_prefix, acl):
    if not os.path.isdir(local_dir):
        print(f"[跳过] {local_dir} 不存在")
        return 0
    n = 0
    for fn in sorted(os.listdir(local_dir)):
        path = os.path.join(local_dir, fn)
        if not os.path.isfile(path):
            continue
        key = f"{remote_prefix}{fn}"
        ext = os.path.splitext(fn)[1]
        headers = {"x-oss-object-acl": acl}
        if ext in CONTENT_TYPE:
            headers["Content-Type"] = CONTENT_TYPE[ext]
        size = os.path.getsize(path)
        oss2.resumable_upload(bucket, key, path, headers=headers,
                              multipart_threshold=20 * 1024 * 1024,
                              part_size=8 * 1024 * 1024, num_threads=4)
        print(f"  ✓ {key:<44} {size / 1024 / 1024:7.2f} MB  [{acl}]")
        n += 1
    return n


def main():
    missing = [k for k, v in {
        "OSS_ENDPOINT": ENDPOINT, "OSS_BUCKET": BUCKET,
        "OSS_KEY_ID": KEY_ID, "OSS_KEY_SECRET": KEY_SECRET,
    }.items() if not v]
    if missing:
        print(f"[跳过上传] 缺少环境变量: {', '.join(missing)}")
        return 0

    bucket = oss2.Bucket(oss2.Auth(KEY_ID, KEY_SECRET), ENDPOINT, BUCKET)

    print(f"上传到 oss://{BUCKET}/  ({ENDPOINT})")
    total = upload_dir(bucket, "open_data/full", "v1/full/", "private")

    if PUBLIC_PREFIX:
        total += upload_dir(bucket, "open_data/public",
                            f"p/{PUBLIC_PREFIX}/", "public-read")
    else:
        print("[提示] 未设置 OSS_PUBLIC_PREFIX，跳过公开区")

    print(f"完成，共 {total} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
