"""
Corrige .env.production no servidor backup para usar o domínio correto
(abasepiaui.cloud ao invés de abasepiaui.com), e reconstrói o frontend.
"""
import sys
import time
import paramiko

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HOST = '72.60.58.181'
SSH_USER = 'root'
SSH_PASS = 'Xdxdapenas12345@'

REPO = '/opt/ABASE/repo'
ENV_FILE = '/opt/ABASE/env/.env.production'
COMPOSE = (
    'docker compose -p abase '
    f'--env-file {ENV_FILE} '
    f'-f {REPO}/deploy/hostinger/docker-compose.prod.yml'
)

# Domínio do servidor backup
BCKP_DOMAIN = 'abasepiaui.cloud'


def ssh_run(client, cmd, timeout=120):
    print(f'\n$ {cmd[:130]}')
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    if out:
        print(out[:3000])
    if err and not any(w in err.lower()[:40] for w in ('warning', '[mysql]', 'deprecation')):
        print(f'[stderr] {err[:400]}')
    return out, err


print('=' * 60)
print('FIX DOMÍNIO BACKUP — abasepiaui.cloud')
print('=' * 60)

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=SSH_USER, password=SSH_PASS, timeout=20)
print('SSH conectado.')

# 1. Ler env atual
print('\n=== [1] LENDO .env.production ATUAL ===')
out_env, _ = ssh_run(client, f'cat {ENV_FILE}')

# 2. Substituir domínios abasepiaui.com → abasepiaui.cloud nas variáveis de URL
print('\n=== [2] CORRIGINDO DOMÍNIO DE abasepiaui.com PARA abasepiaui.cloud ===')

# Linhas que devem usar o domínio do backup
lines = out_env.splitlines()
new_lines = []
for line in lines:
    # Substituir apenas nas variáveis de URL/domínio — não nas credenciais
    if any(line.startswith(k) for k in (
        'NEXT_PUBLIC_API_URL=',
        'INTERNAL_API_URL=',
        'DOMAIN=',
        'CORS_ALLOWED_ORIGINS=',
        'CSRF_TRUSTED_ORIGINS=',
    )):
        new_line = line.replace('abasepiaui.com', BCKP_DOMAIN)
        if new_line != line:
            print(f'   {line}')
            print(f'→  {new_line}')
        new_lines.append(new_line)
    elif line.startswith('ALLOWED_HOSTS='):
        # Garantir que ambos domínios estão presentes
        current = line[len('ALLOWED_HOSTS='):]
        hosts = [h.strip() for h in current.split(',') if h.strip()]
        for h in [BCKP_DOMAIN, f'www.{BCKP_DOMAIN}', 'abasepiaui.com', 'www.abasepiaui.com', 'backend', 'localhost']:
            if h not in hosts:
                hosts.append(h)
        new_lines.append('ALLOWED_HOSTS=' + ','.join(hosts))
    else:
        new_lines.append(line)

new_env = '\n'.join(new_lines)

# 3. Gravar novo env no servidor backup
print('\n=== [3] GRAVANDO NOVO .env.production ===')
# Usar sftp para gravar o arquivo sem problemas de escape
sftp = client.open_sftp()
with sftp.open(ENV_FILE, 'w') as f:
    f.write(new_env + '\n')
sftp.close()
print('   Arquivo gravado via SFTP.')

# Confirmar
ssh_run(client, f'grep -E "DOMAIN=|NEXT_PUBLIC_API_URL=|ALLOWED_HOSTS=" {ENV_FILE}')

# 4. Rebuild do frontend com domínio correto
print('\n=== [4] REBUILD FRONTEND (--no-cache) ===')
ssh_run(client,
    f'cd {REPO} && {COMPOSE} build --no-cache frontend',
    timeout=600)

# 5. Recriar container frontend
print('\n=== [5] RECREATE FRONTEND ===')
ssh_run(client,
    f'{COMPOSE} up -d --force-recreate --no-deps frontend',
    timeout=60)

# 6. Recriar backend para recarregar CORS/CSRF
print('\n=== [6] RECREATE BACKEND ===')
ssh_run(client,
    f'{COMPOSE} up -d --force-recreate --no-deps backend celery',
    timeout=60)

# 7. Status final
print('\nAguardando 15s...')
time.sleep(15)
print('\n=== [7] STATUS FINAL ===')
ssh_run(client,
    "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep abase")

print('\n=== [8] LOGS FRONTEND (tail 10) ===')
ssh_run(client, 'docker logs abase-frontend-prod --tail 10 2>&1')

print('\n=== [9] LOGS BACKEND (tail 10) ===')
ssh_run(client, 'docker logs abase-backend-prod --tail 10 2>&1')

client.close()
print('\n' + '=' * 60)
print('FIX CONCLUÍDO — Frontend aponta para abasepiaui.cloud')
print('=' * 60)
