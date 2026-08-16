import paramiko
HOST="222.211.217.7"; PORT=10022; USER="root"; PW="8vFdXMt@&s8cXM9D"
LOCAL=r"D:\workbuddy\2026-08-06-15-31-48\start_vlm.sh"
REMOTE="/opt/math-vlm/start_vlm.sh"
cli=paramiko.SSHClient(); cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST,port=PORT,username=USER,password=PW,timeout=10)
sftp=cli.open_sftp()
sftp.put(LOCAL,REMOTE)
sftp.chmod(REMOTE,0o755)
sftp.close()
stdin,stdout,stderr=cli.exec_command("ls -l /opt/math-vlm/start_vlm.sh && head -3 /opt/math-vlm/start_vlm.sh")
print(stdout.read().decode())
cli.close()
