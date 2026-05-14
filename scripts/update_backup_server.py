"""
Atualiza servidor backup abasepiaui.cloud com código e env do servidor oficial.

REGRAS:
  - Código: branch abase-prod-bckp (mesmo HEAD que abaseprod no oficial)
  - Env: copiado do servidor oficial, mas com domínios ajustados para o backup:
      abasepiaui.com  →  abasepiaui.cloud
    (DOMAIN, NEXT_PUBLIC_API_URL, CORS, CSRF, ALLOWED_HOSTS)
  - Credenciais (SECRET_KEY, MySQL, JWT): vindas do servidor oficial — IGUAIS
  - Senhas de usuários web: vêm do dump do banco, NÃO resetar manualmente

Servidores:
  Oficial:  72.60.48.163  user=deploy  chave=~/.ssh/abase_deploy
  Backup:   72.60.58.181  user=root    senha=Xdxdapenas12345@
"""
import sys
import time
import paramiko

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Servidor oficial (produção) ──────────────────────────────
PROD_HOST   = '72.60.48.163'
PROD_USER   = 'deploy'
PROD_KEY    = r'C:\Users\helciovenancio\.ssh\abase_deploy'

# ── Servidor backup ──────────────────────────────────────────
BCKP_HOST   = '72.60.58.181'
BCKP_USER   = 'root'
BCKP_PASS   = 'Xdxdapenas12345@'

# ── Domínios ─────────────────────────────────────────────────
PROD_DOMAIN = 'abasepiaui.com'
BCKP_DOMAIN = 'abasepiaui.cloud'

# ── Paths (iguais nos dois servidores) ───────────────────────
REPO     = '/opt/ABASE/repo'
ENV_FILE = '/opt/ABASE/env/.env.production'
COMPOSE  = (
    'docker compose -p abase '
    f'--env-file {ENV_FILE} '
    f'-f {REPO}/deploy/hostinger/docker-compose.prod.yml'
)

BCKP_BRANCH = 'abase-prod-bckp'


def ssh_run(client, cmd, timeout=120, label=None):
    print(f'\n$ {(label or cmd)[:140]}')
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    if out:
        print(out[:4000])
    noise = ('warning', '[mysql]', 'deprecation', 'version` is obsolete')
    if err and not any(n in err.lower()[:60] for n in noise):
        print(f'[stderr] {err[:600]}')
    return out, err


def connect_prod():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(PROD_HOST, username=PROD_USER, key_filename=PROD_KEY, timeout=20)
    return c


def connect_bckp():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(BCKP_HOST, username=BCKP_USER, password=BCKP_PASS, timeout=20)
    return c


def adapt_env_for_backup(env_text: str) -> str:
    """
    Recebe o .env.production do servidor oficial e ajusta para o servidor backup:
    - Substitui domínio PROD_DOMAIN → BCKP_DOMAIN nas vars de URL/CORS/CSRF/DOMAIN
    - Garante que ALLOWED_HOSTS inclui ambos os domínios
    - Credenciais (SECRET_KEY, MySQL, JWT) ficam intactas
    """
    URL_KEYS = {
        'NEXT_PUBLIC_API_URL=',
        'INTERNAL_API_URL=',
        'DOMAIN=',
        'CORS_ALLOWED_ORIGINS=',
        'CSRF_TRUSTED_ORIGINS=',
    }
    new_lines = []
    for line in env_text.splitlines():
        key = next((k for k in URL_KEYS if line.startswith(k)), None)
        if key:
            new_line = line.replace(PROD_DOMAIN, BCKP_DOMAIN)
            if new_line != line:
                print(f'   ajuste: {line}')
                print(f'       → {new_line}')
            new_lines.append(new_line)
        elif line.startswith('ALLOWED_HOSTS='):
            current = line[len('ALLOWED_HOSTS='):]
            hosts = [h.strip() for h in current.split(',') if h.strip()]
            for extra in [BCKP_DOMAIN, f'www.{BCKP_DOMAIN}',
                          PROD_DOMAIN, f'www.{PROD_DOMAIN}',
                          'backend', 'localhost']:
                if extra not in hosts:
                    hosts.append(extra)
            new_lines.append('ALLOWED_HOSTS=' + ','.join(hosts))
        else:
            new_lines.append(line)
    return '\n'.join(new_lines)


# ─────────────────────────────────────────────────────────────
print('=' * 60)
print('UPDATE SERVIDOR BACKUP — abasepiaui.cloud')
print('=' * 60)

# ── 1. Ler env e commit do servidor oficial ──────────────────
print('\n=== [1] COLETANDO DADOS DO SERVIDOR OFICIAL ===')
prod = connect_prod()
env_oficial, _ = ssh_run(prod, f'cat {ENV_FILE}', label='cat .env.production (oficial)')
if not env_oficial:
    print('ERRO: não foi possível ler .env do servidor oficial.')
    prod.close()
    sys.exit(1)
prod_commit, _ = ssh_run(prod, f'cd {REPO} && git log --oneline -1')
prod.close()
print(f'\nCommit no servidor oficial: {prod_commit}')

# ── 2. Adaptar env para o backup ────────────────────────────
print('\n=== [2] ADAPTANDO .env.production PARA BACKUP ===')
env_backup = adapt_env_for_backup(env_oficial)

# ── 3. Aplicar env no servidor backup via SFTP ──────────────
print('\n=== [3] APLICANDO .env.production NO SERVIDOR BACKUP ===')
bckp = connect_bckp()
sftp = bckp.open_sftp()
sftp.mkdir('/opt/ABASE/env') if not any(
    True for _ in [None]
    if True
) else None
try:
    sftp.stat('/opt/ABASE/env')
except FileNotFoundError:
    sftp.mkdir('/opt/ABASE/env')
with sftp.open(ENV_FILE, 'w') as f:
    f.write(env_backup + '\n')
sftp.close()
print('   .env.production gravado via SFTP.')
ssh_run(bckp, f'grep -E "^DOMAIN=|^NEXT_PUBLIC_API_URL=|^ALLOWED_HOSTS=" {ENV_FILE}')

# ── 4. Verificar commit atual no backup ─────────────────────
print('\n=== [4] COMMIT ATUAL NO SERVIDOR BACKUP ===')
ssh_run(bckp, f'cd {REPO} && git log --oneline -3')

# ── 5. Git status — abortar se sujo ─────────────────────────
print('\n=== [5] GIT STATUS ===')
out_status, _ = ssh_run(bckp, f'cd {REPO} && git status --short')
tracked_changes = [l for l in out_status.splitlines() if not l.startswith('?')]
if tracked_changes:
    print(f'[AVISO] Arquivos rastreados modificados: {tracked_changes}')
    print('Abortando para não perder alterações.')
    bckp.close()
    sys.exit(1)
print('Working tree OK.')

# ── 6. Fetch + pull ──────────────────────────────────────────
print('\n=== [6] FETCH + PULL ===')
ssh_run(bckp, f'cd {REPO} && git fetch origin')
ssh_run(bckp, f'cd {REPO} && git checkout {BCKP_BRANCH}')
ssh_run(bckp, f'cd {REPO} && git pull --ff-only origin {BCKP_BRANCH}')
print('\nCommit após pull:')
ssh_run(bckp, f'cd {REPO} && git log --oneline -1')

# ── 7. Build backend + celery (--no-cache) ──────────────────
print('\n=== [7] BUILD BACKEND + CELERY (--no-cache) ===')
ssh_run(bckp,
    f'cd {REPO} && {COMPOSE} build --no-cache backend celery',
    timeout=600, label='docker compose build --no-cache backend celery')

# ── 8. Build frontend (--no-cache) ──────────────────────────
print('\n=== [8] BUILD FRONTEND (--no-cache) ===')
ssh_run(bckp,
    f'cd {REPO} && {COMPOSE} build --no-cache frontend',
    timeout=600, label='docker compose build --no-cache frontend')

# ── 9. Recriar containers ────────────────────────────────────
print('\n=== [9] UP --force-recreate ===')
ssh_run(bckp,
    f'{COMPOSE} up -d --force-recreate --no-deps backend celery',
    timeout=60)
time.sleep(8)
ssh_run(bckp,
    f'{COMPOSE} up -d --force-recreate --no-deps frontend',
    timeout=60)

# ── 10. Migrate ──────────────────────────────────────────────
print('\n=== [10] MIGRATE ===')
time.sleep(12)
ssh_run(bckp,
    f'{COMPOSE} exec -T backend python manage.py migrate --noinput',
    timeout=60)

# ── 11. Django check ─────────────────────────────────────────
print('\n=== [11] DJANGO CHECK ===')
ssh_run(bckp,
    f'{COMPOSE} exec -T backend python manage.py check',
    timeout=30)

# ── 12. Status final ─────────────────────────────────────────
print('\n=== [12] STATUS FINAL ===')
time.sleep(8)
ssh_run(bckp,
    "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep abase")

print('\n=== [13] LOGS BACKEND (tail 15) ===')
ssh_run(bckp, 'docker logs abase-backend-prod --tail 15 2>&1')

print('\n=== [14] LOGS FRONTEND (tail 8) ===')
ssh_run(bckp, 'docker logs abase-frontend-prod --tail 8 2>&1')

bckp.close()

print('\n' + '=' * 60)
print('UPDATE CONCLUÍDO — abasepiaui.cloud')
print(f'Commit: {prod_commit}')
print(f'Frontend aponta para: https://{BCKP_DOMAIN}/api/v1')
print('Senhas de usuários: mesmas da produção (hash do dump do banco)')
print('=' * 60)
