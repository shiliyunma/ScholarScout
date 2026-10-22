# -*- coding: utf-8 -*-
"""TLS 中间人探针 —— QQ/模型接口连不上、或日志出现 CERTIFICATE_VERIFY_FAILED 时跑它。

用法： python tls_probe.py
判读： issuer 是真证书（GlobalSign / TrustAsia 等）→ 此刻无中间人，故障已恢复；
      出现 self-signed / 火绒 / 360 / Kaspersky / Clash / mitm 之类 → 有东西在做 HTTPS 拦截。
"""
import ssl
import socket

HOSTS = ["bots.qq.com", "api.sgroup.qq.com", "api.deepseek.com"]


def probe(host):
    print("=" * 12, host, "=" * 12)
    try:
        s = socket.create_connection((host, 443), 10)
        ss = ssl.create_default_context().wrap_socket(s, server_hostname=host)
        c = ss.getpeercert()
        iss = dict(x[0] for x in c.get("issuer", ()))
        print("  [\u6821\u9a8c\u5f00\u542f] OK", ss.version(),
              "| issuer:", iss.get("organizationName"), "/", iss.get("commonName"))
        return
    except Exception as e:
        print("  [\u6821\u9a8c\u5f00\u542f] \u5931\u8d25 ->", type(e).__name__, str(e)[:160])
    # 关掉校验，把实际呈现的证书挖出来（这才是关键信息）
    try:
        s2 = socket.create_connection((host, 443), 10)
        ss2 = ssl._create_unverified_context().wrap_socket(s2, server_hostname=host)
        pem = ssl.DER_cert_to_PEM_cert(ss2.getpeercert(binary_form=True))
        print("  [\u5b9e\u9645\u8bc1\u4e66] \u5982\u4e0b\uff0c\u628a issuer \u53d1\u7ed9\u7528\u6237\u5c31\u80fd\u786e\u5b9a\u662f\u8c01\u5728\u62e6\u622a\uff1a")
        print("  " + pem.replace("\n", "\n  ")[:900])
    except Exception as e2:
        print("  [\u5173\u6821\u9a8c\u4e5f\u5931\u8d25] ->", type(e2).__name__, str(e2)[:160])


if __name__ == "__main__":
    for h in HOSTS:
        probe(h)
