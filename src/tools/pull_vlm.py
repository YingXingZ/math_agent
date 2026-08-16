import paramiko

HOST, PORT, USER, PW = "222.211.217.7", 10022, "root", "8vFdXMt@&s8cXM9D"
REMOTE = "/opt/math-vlm/server_vlm_service.py"
LOCAL = r"D:\workbuddy\2026-08-06-15-31-48\server_vlm_service.py"

t = paramiko.Transport((HOST, PORT))
t.connect(username=USER, password=PW)
sftp = paramiko.SFTPClient.from_transport(t)
print("[get] ->", REMOTE)
sftp.get(REMOTE, LOCAL)
sftp.close()
t.close()
print("[done] saved to", LOCAL)
