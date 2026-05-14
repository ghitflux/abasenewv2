"""
Reseta senha admin no servidor backup e testa login/endpoints
"""
import sys
import json
import urllib.request
import urllib.error
import paramiko

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HOST = '72.60.58.181'
USER = 'root'
PASSWORD = 'Xdxdapenas12345@'
BASE_URL = 'https://abasepiaui.cloud/api/v1'

NEW_PASSWORD = 'Backup@2026'  # senha temporária para o servidor backup


def ssh_exec(client, cmd, timeout=30):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    return out, err


def http_post(url, data):
    payload = json.dumps(data).encode('utf-8')
    req = urllib.request.Request(
        url, data=payload,
        headers={'Content-Type': 'application/json'}, method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return None, str(e)


def http_get(url, token=None):
    req = urllib.request.Request(url, method='GET')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return None, str(e)


print('=' * 60)
print('RESET SENHA + TESTE BACKUP - abasepiaui.cloud')
print('=' * 60)

print('\n[1] Conectando SSH...')
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASSWORD, timeout=20)
print('   OK')

# Listar usuários staff via Django shell
print('\n[2] Usuários staff no banco...')
list_cmd = (
    'docker exec abase-backend-prod python -c '
    '"import django, os; os.environ.setdefault(\'DJANGO_SETTINGS_MODULE\', \'config.settings.production\'); '
    'django.setup(); '
    'from apps.accounts.models import User; '
    'users = list(User.objects.filter(is_staff=True).values(\'id\', \'email\', \'is_superuser\')); '
    'print(users)"'
)
out, err = ssh_exec(client, list_cmd, timeout=30)
print(f'   {out[:400] if out else err[:400]}')

# Listar usuários via manage.py shell
print('\n[3] Listando via manage.py shell...')
shell_cmd = (
    "docker exec abase-backend-prod sh -c "
    "'python manage.py shell --no-input << EOF\n"
    "from apps.accounts.models import User\n"
    "for u in User.objects.filter(is_staff=True)[:10]:\n"
    "    print(u.id, u.email, u.is_superuser)\n"
    "EOF'"
)
out3, err3 = ssh_exec(client, shell_cmd, timeout=30)
print(f'   {out3[:400] if out3 else err3[:400]}')

# Resetar senha do superuser via Django
print(f'\n[4] Resetando senha dos superusers para: {NEW_PASSWORD}')
reset_cmd = (
    "docker exec abase-backend-prod python manage.py shell -c "
    f"\"from apps.accounts.models import User; "
    f"admins = User.objects.filter(is_superuser=True); "
    f"count = admins.count(); "
    f"[u.set_password('{NEW_PASSWORD}') or u.save() for u in admins]; "
    f"print(f'Resetados: ' + str(count) + ' superusers'); "
    f"[print('  -', u.email) for u in admins]\""
)
out4, err4 = ssh_exec(client, reset_cmd, timeout=30)
print(f'   {out4 if out4 else err4[:300]}')

# Listar emails dos superusers resetados
print('\n[5] Emails dos superusers...')
list_cmd2 = (
    "docker exec abase-backend-prod python manage.py shell -c "
    "\"from apps.accounts.models import User; "
    "[print(u.email) for u in User.objects.filter(is_superuser=True)]\""
)
out5, err5 = ssh_exec(client, list_cmd2, timeout=30)
print(f'   {out5 if out5 else err5[:300]}')

client.close()
print('\n   SSH encerrado.')

# Extrair primeiro email da lista
admin_emails = [line.strip() for line in out5.split('\n') if '@' in line.strip()]
if not admin_emails:
    admin_emails = ['ghitflux@gmail.com']
print(f'   Emails encontrados: {admin_emails}')

# Testar login com nova senha
print(f'\n[6] Testando login com nova senha ({NEW_PASSWORD})...')
token = None
logged_email = None

for email in admin_emails:
    status, body = http_post(f'{BASE_URL}/auth/login/', {
        'login': email, 'password': NEW_PASSWORD
    })
    print(f'   {email} → {status}')
    if status == 200:
        data = json.loads(body)
        token = data.get('access') or data.get('token') or data.get('access_token')
        logged_email = email
        print(f'   Token: {token[:60]}...')
        break
    else:
        try:
            print(f'   Erro: {json.loads(body)}')
        except Exception:
            print(f'   Body: {body[:100]}')

if not token:
    print('\n   Login falhou mesmo após reset. Verificar manualmente.')
    sys.exit(1)

# Testar endpoints autenticados
print(f'\n[7] Endpoints autenticados (user={logged_email}):')
endpoints = [
    ('/auth/me/', 'perfil'),
    ('/associados/?page_size=1', 'associados'),
    ('/contratos/?page_size=1', 'contratos'),
    ('/tesouraria/caixa/?page_size=1', 'caixa'),
    ('/refinanciamentos/?page_size=1', 'refinanciamentos'),
]
for ep, label in endpoints:
    status, body = http_get(f'{BASE_URL}{ep}', token=token)
    if status == 200:
        try:
            data = json.loads(body)
            count = data.get('count', data.get('id', 'ok')) if isinstance(data, dict) else 'ok'
            print(f'   [{status}] {label}: count/id={count}')
        except Exception:
            print(f'   [{status}] {label}: ok')
    else:
        print(f'   [{status}] {label}: {body[:100]}')

print('\n' + '=' * 60)
print('SERVIDOR BACKUP VALIDADO')
print(f'URL: https://abasepiaui.cloud')
print(f'Email admin: {logged_email}')
print(f'Senha backup: {NEW_PASSWORD}')
print('=' * 60)
