#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backup, upload, and restart the server VLM service (safe restart)."""
import paramiko, sys, time

HOST, PORT, USER, PW = "222.211.217.7", 10022, "root", "8vFdXMt@&s8cXM9D"
LOCAL = r"D:\workbuddy\2026-08-06-15-31-48\server_vlm_service.py"
REMOTE = "/opt/math-vlm/server_vlm_service.py"
START = "/opt/math-vlm/start_vlm.sh"


def ssh(cmd, timeout=60):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PW, timeout=10,
              banner_timeout=10, auth_timeout=10)
    try:
        stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    finally:
        c.close()


def main():
    # 1) backup remote
    ts = time.strftime("%Y%m%d-%H%M%S")
    rc, out, err = ssh(f"cp -f {REMOTE} {REMOTE}.bak-{ts} && echo backed_up")
    print("[backup]", rc, out.strip(), err.strip())

    # 2) upload via sftp
    t = paramiko.Transport((HOST, PORT))
    t.connect(username=USER, password=PW)
    sftp = paramiko.SFTPClient.from_transport(t)
    sftp.put(LOCAL, REMOTE)
    sftp.close()
    t.close()
    print("[upload] OK ->", REMOTE)

    # 3) compile on server
    rc, out, err = ssh("cd /opt/math-vlm && /opt/math-vlm/bin/python3 -m py_compile server_vlm_service.py && echo COMPILE_OK")
    print("[compile]", rc, out.strip(), err.strip())
    if rc != 0:
        print("编译失败，中止重启"); sys.exit(1)

    # 3b) push start_vlm.sh so future restarts use the orphan-safe fuser kill
    try:
        t2 = paramiko.Transport((HOST, PORT))
        t2.connect(username=USER, password=PW)
        sftp2 = paramiko.SFTPClient.from_transport(t2)
        sftp2.put(r"D:\workbuddy\2026-08-06-15-31-48\start_vlm.sh", "/opt/math-vlm/start_vlm.sh")
        sftp2.close(); t2.close()
        ssh("chmod +x /opt/math-vlm/start_vlm.sh")
        print("[start_vlm.sh] pushed + chmod +x")
    except Exception as e:
        print("[start_vlm.sh] push failed (non-fatal):", e)

    # 4) restart via start_vlm.sh (it internally pkills the old service safely,
    #    then launches the new one fully detached with setsid).
    rc, out, err = ssh(f"bash {START}")
    print("[restart]", rc, out.strip(), err.strip())

    # 5) wait for the HTTP endpoint to come up (model loads lazily on first call)
    for i in range(15):
        time.sleep(3)
        rc, out, err = ssh("curl -s -m 5 http://127.0.0.1:18080/health")
        if '"ok":true' in out:
            print(f"[health] endpoint up after ~{(i+1)*3}s")
            return
        print(f"  waiting ({i+1}) {out.strip()[:60]}")
    print("[health] TIMEOUT")


if __name__ == "__main__":
    main()
