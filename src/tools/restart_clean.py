#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clean restart: kill whoever holds port 18080 (handles orphaned workers),
wait for the port to be free, then start a fresh uvicorn detached."""
import paramiko, time

HOST, PORT, USER, PW = "222.211.217.7", 10022, "root", "8vFdXMt@&s8cXM9D"
REMOTE_DIR = "/opt/math-vlm"
LOG = f"{REMOTE_DIR}/uvicorn_0000.log"


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
    # 1) kill whatever holds port 18080 (port-based, no cmdline pattern)
    rc, out, err = ssh("fuser -k 18080/tcp 2>/dev/null; true")
    print("[fuser-k]", rc, (out + err).strip())

    # 2) wait for port to be free
    for i in range(20):
        rc, out, err = ssh("ss -ltn 2>/dev/null | grep -q ':18080 ' && echo BUSY || echo FREE")
        if "FREE" in out:
            print(f"[port] free after ~{(i + 1)}s")
            break
        time.sleep(1)
    else:
        print("[port] STILL BUSY after 20s -- aborting")
        return

    # 3) belt-and-suspenders: also pkill any lingering uvicorn workers
    ssh("pkill -9 -f 'server_vlm_service:app' 2>/dev/null; true")
    time.sleep(2)

    # 4) start fresh, fully detached
    start_cmd = (
        f"cd {REMOTE_DIR} && setsid nohup /opt/math-vlm/bin/python3 "
        f"./bin/uvicorn server_vlm_service:app --host 0.0.0.0 --port 18080 --workers 4 "
        f"</dev/null >{LOG} 2>&1 & echo started_new"
    )
    rc, out, err = ssh(start_cmd)
    print("[start]", rc, out.strip(), err.strip())

    # 5) poll health
    for i in range(15):
        time.sleep(3)
        rc, out, err = ssh("curl -s -m 5 http://127.0.0.1:18080/health")
        print(f"  health[{i}] ->", out.strip()[:120])
        if '"ok":true' in out:
            print(f"[health] up after ~{(i + 1) * 3}s")
            return
    print("[health] TIMEOUT")


if __name__ == "__main__":
    main()
