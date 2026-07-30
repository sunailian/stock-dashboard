#!/usr/bin/env python3.13
"""OSS 静态站点上传 — 将 stock-dashboard 部署到阿里云 OSS"""
import os, sys, glob
import oss2

BUCKET = "study-attachments"
ENDPOINT = "oss-cn-beijing.aliyuncs.com"
LOCAL_DIR = os.path.expanduser("~/Documents/workspace/stock-dashboard")
OSS_PREFIX = "stock-dashboard/"  # OSS 路径前缀

def upload():
    ak = os.getenv("OSS_ACCESS_KEY_ID") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    sk = os.getenv("OSS_ACCESS_KEY_SECRET") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    if not ak or not sk:
        print("❌ 请设置 OSS_ACCESS_KEY_ID 和 OSS_ACCESS_KEY_SECRET")
        sys.exit(1)

    auth = oss2.Auth(ak, sk)
    bucket = oss2.Bucket(auth, ENDPOINT, BUCKET)

    # 上传 HTML、CSS、JS 等静态文件
    files = glob.glob(f"{LOCAL_DIR}/**", recursive=True)
    uploaded = 0
    for fp in files:
        if not os.path.isfile(fp): continue
        rel = os.path.relpath(fp, LOCAL_DIR)
        if rel.startswith(".git") or rel.endswith(".pyc"): continue
        key = OSS_PREFIX + rel
        bucket.put_object_from_file(key, fp)
        print(f"  ✅ {key}")
        uploaded += 1

    # 设置 index.html 为默认首页
    bucket.put_object_from_file(OSS_PREFIX + "index.html", f"{LOCAL_DIR}/index.html")
    print(f"\n🎉 上传完成: {uploaded} 个文件")
    print(f"   访问: https://{BUCKET}.{ENDPOINT}/{OSS_PREFIX}index.html")

if __name__ == "__main__":
    upload()
