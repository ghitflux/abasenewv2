"""
Sincroniza credenciais MySQL do servidor backup com as do servidor oficial.
Executa ALTER USER para atualizar senha do usuário 'abase' no MySQL do backup.
"""
import sys
import time
import paramiko

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HOST = '72.60.58.181'
SSH_USER = 'root'
SSH_PASS = 'Xdxdapenas12345@'

MYSQL_CONTAINER = 'abase-mysql-prod'
ENV_FILE = '/opt/ABASE/env/.env.production'
REPO = '/opt/ABASE/repo'
COMPOSE = (
    'docker compose -p abase '
    f'--env-file {ENV_FILE} '
    f'-f {REPO}/deploy/hostinger/docker-compose.prod.yml'
)

# Senha nova (do servidor oficial) que agora está no .env.production
NEW_ABASE_PASS  = "eaSrW4*p6V%I26zoAQlLGSYPOzSK"
NEW_ROOT_PASS   = "WkjZ40M9T@Vi9hj*Ddq26elZ4@HY"

# Possíveis senhas root antigas (tentamos em sequência)
OLD_ROOT_CANDIDATES = [
    "OY8wCNEHX22uT2Gw_ZmjYznB8GAjWGHZkPG0-wBmkIU",  # senha inicial do backup
    "WkjZ40M9T@Vi9hj*Ddq26elZ4@HY",                  # já pode ter sido atualizada
    "RPxCd5iEGbI-7yVFuC3w9Jk41EFYFPHtChAQPQxy73M",   # senha user abase antigo
]


def ssh_run(client, cmd, timeout=60):
    print(f'\n$ {cmd[:130]}')
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    if out:
        print(out[:2000])
    if err and not any(w in err.lower()[:40] for w in ('warning', '[mysql]', 'mysql: [warning]')):
        print(f'[stderr] {err[:400]}')
    return out, err


print('=' * 60)
print('FIX MySQL CREDENTIALS — abasepiaui.cloud')
print('=' * 60)

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=SSH_USER, password=SSH_PASS, timeout=20)
print('SSH conectado.')

# 1. Descobrir qual senha root funciona no MySQL do backup
print('\n=== [1] DESCOBRINDO SENHA ROOT DO MySQL BACKUP ===')
working_root_pass = None
for candidate in OLD_ROOT_CANDIDATES:
    test_cmd = f"docker exec {MYSQL_CONTAINER} mysql -uroot -p'{candidate}' -e 'SELECT 1;' 2>/dev/null"
    out, err = client.exec_command(test_cmd)[1].read().decode('utf-8', errors='replace'), ''
    # Tentativa direta
    stdin, stdout, stderr = client.exec_command(
        f"docker exec {MYSQL_CONTAINER} mysql -uroot -p'{candidate}' -e 'SELECT 1;' 2>&1"
    )
    result = stdout.read().decode('utf-8', errors='replace') + stderr.read().decode('utf-8', errors='replace')
    if 'Access denied' not in result and 'ERROR' not in result:
        working_root_pass = candidate
        print(f'   Senha root encontrada: {candidate[:20]}...')
        break
    else:
        print(f'   Candidata {candidate[:20]}... — falhou')

if not working_root_pass:
    print('\nNão foi possível autenticar como root no MySQL.')
    print('Tentando via mysqladmin...')
    client.close()
    sys.exit(1)

# 2. Atualizar senha do usuário 'abase'
print(f'\n=== [2] ATUALIZANDO SENHA DO USUÁRIO abase ===')
sql_abase = (
    f"ALTER USER 'abase'@'%' IDENTIFIED BY '{NEW_ABASE_PASS}'; "
    f"FLUSH PRIVILEGES;"
)
cmd_abase = f"docker exec {MYSQL_CONTAINER} mysql -uroot -p'{working_root_pass}' -e \"{sql_abase}\" 2>&1"
out2, _ = ssh_run(client, cmd_abase)

# 3. Atualizar senha root
print(f'\n=== [3] ATUALIZANDO SENHA ROOT ===')
sql_root = (
    f"ALTER USER 'root'@'%' IDENTIFIED BY '{NEW_ROOT_PASS}'; "
    f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{NEW_ROOT_PASS}'; "
    f"FLUSH PRIVILEGES;"
)
cmd_root = f"docker exec {MYSQL_CONTAINER} mysql -uroot -p'{working_root_pass}' -e \"{sql_root}\" 2>&1"
out3, _ = ssh_run(client, cmd_root)

# 4. Verificar se a nova senha funciona
print(f'\n=== [4] VERIFICANDO NOVA SENHA abase ===')
stdin, stdout, stderr = client.exec_command(
    f"docker exec {MYSQL_CONTAINER} mysql -uabase -p'{NEW_ABASE_PASS}' abase_v2 "
    f"-e 'SELECT COUNT(*) FROM accounts_user;' 2>&1"
)
result = stdout.read().decode('utf-8', errors='replace') + stderr.read().decode('utf-8', errors='replace')
print(f'   {result.strip()}')
if 'Access denied' in result:
    print('ERRO: Senha ainda não funciona. Verificar manualmente.')
    client.close()
    sys.exit(1)
print('Senha do usuário abase atualizada e verificada.')

# 5. Recriar backend e celery para carregar nova credencial
print(f'\n=== [5] RECRIANDO BACKEND + CELERY ===')
ssh_run(client,
    f'{COMPOSE} up -d --force-recreate --no-deps backend celery',
    timeout=60)

# 6. Aguardar e verificar
print('\nAguardando 15s para containers iniciarem...')
time.sleep(15)

print(f'\n=== [6] STATUS DOS CONTAINERS ===')
ssh_run(client,
    "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep abase")

# 7. Migrate
print(f'\n=== [7] MIGRATE ===')
ssh_run(client,
    f'{COMPOSE} exec -T backend python manage.py migrate --noinput',
    timeout=60)

# 8. Django check
print(f'\n=== [8] DJANGO CHECK ===')
ssh_run(client,
    f'{COMPOSE} exec -T backend python manage.py check',
    timeout=30)

# 9. Logs do backend
print(f'\n=== [9] LOGS BACKEND (tail 15) ===')
ssh_run(client, 'docker logs abase-backend-prod --tail 15 2>&1')

client.close()
print('\n' + '=' * 60)
print('FIX CONCLUÍDO — Credenciais MySQL sincronizadas com oficial')
print('=' * 60)
