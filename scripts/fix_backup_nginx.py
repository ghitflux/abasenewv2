"""
Corrige nginx.conf no servidor backup para usar abasepiaui.cloud
e recarrega o container nginx.
"""
import sys, io, paramiko

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('72.60.58.181', username='root', password='Xdxdapenas12345@', timeout=20)

def run(cmd, timeout=30):
    print(f'\n$ {cmd[:130]}')
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    if out: print(out[:4000])
    if err and not any(w in err.lower()[:40] for w in ('warning', 'note')):
        print(f'[err] {err[:400]}')
    return out, err

NGINX_CONF_PATH = '/opt/ABASE/repo/deploy/hostinger/nginx/nginx.conf'

# 1. Ver certs disponíveis
print('=== [1] CERTIFICADOS SSL DISPONÍVEIS ===')
run('ls /opt/ABASE/data/certbot/conf/live/ 2>/dev/null || echo "sem certbot"')
run('ls /etc/letsencrypt/live/ 2>/dev/null || echo "nao existe"')

# 2. Ler nginx.conf atual
print('\n=== [2] LENDO nginx.conf ATUAL ===')
sftp = client.open_sftp()
with sftp.open(NGINX_CONF_PATH, 'r') as f:
    original = f.read().decode('utf-8', errors='replace')
print(original[:200] + '...')

# 3. Verificar qual domínio de cert existe para o backup
print('\n=== [3] DOMÍNIO DO CERT ===')
out, _ = run('ls /opt/ABASE/data/certbot/conf/live/ 2>/dev/null')
cert_domain = 'abasepiaui.cloud'  # padrão
if out:
    for line in out.splitlines():
        line = line.strip()
        if line and 'README' not in line:
            cert_domain = line
            break
print(f'   Domínio do certificado: {cert_domain}')

# 4. Gerar nginx.conf para o backup
print(f'\n=== [4] GERANDO nginx.conf PARA {cert_domain} ===')
adapted = (
    original
    .replace('abasepiaui.com', cert_domain)
)
print('Primeiras linhas do config adaptado:')
for line in adapted.splitlines()[:15]:
    print(f'  {line}')

# 5. Gravar nginx.conf no backup via SFTP
print('\n=== [5] GRAVANDO nginx.conf NO SERVIDOR BACKUP ===')
with sftp.open(NGINX_CONF_PATH, 'w') as f:
    f.write(adapted)
sftp.close()
print('   Gravado.')

# 6. Testar config do nginx
print('\n=== [6] TESTANDO nginx -t ===')
run('docker exec abase-nginx-prod nginx -t 2>&1')

# 7. Reload do nginx
print('\n=== [7] RELOAD NGINX ===')
run('docker exec abase-nginx-prod nginx -s reload 2>&1')

# 8. Verificar acesso interno
print('\n=== [8] TESTANDO ACESSO INTERNO ===')
run(f'curl -sk https://{cert_domain}/api/v1/health/ -o /dev/null -w "%{{http_code}}" 2>&1')
run(f'curl -sk https://{cert_domain}/ -o /dev/null -w "%{{http_code}}" 2>&1')

# 9. Logs do nginx
print('\n=== [9] LOGS NGINX ===')
run('docker logs abase-nginx-prod --tail 20 2>&1', timeout=15)

client.close()
print('\nFIM')
