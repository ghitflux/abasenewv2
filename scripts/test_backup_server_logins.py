"""
Testa logins e endpoints no servidor backup abasepiaui.cloud
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

MYSQL_CONTAINER = 'abase-mysql-prod'
MYSQL_USER = 'abase'
MYSQL_PASS = 'RPxCd5iEGbI-7yVFuC3w9Jk41EFYFPHtChAQPQxy73M'
MYSQL_DB = 'abase_v2'


def ssh_exec(client, cmd, timeout=30):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    return out, err


def http_post(url, data):
    payload = json.dumps(data).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        return e.code, body
    except Exception as e:
        return None, str(e)


def http_get(url, token=None):
    req = urllib.request.Request(url, method='GET')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        return e.code, body
    except Exception as e:
        return None, str(e)


print('=' * 60)
print('TESTE SERVIDOR BACKUP - abasepiaui.cloud')
print('=' * 60)

# 1. Conectar SSH
print('\n[1] Conectando SSH...')
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASSWORD, timeout=20)
print('   Conectado OK')

# 2. Status dos containers
print('\n[2] Status dos containers:')
out, _ = ssh_exec(client, "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep abase")
print(out)

# 3. Buscar usuários admin/staff via mysql root
print('\n[3] Buscando usuários admin/staff no banco...')
cmd3 = (
    f"docker exec {MYSQL_CONTAINER} mysql "
    f"-uroot -p'OY8wCNEHX22uT2Gw_ZmjYznB8GAjWGHZkPG0-wBmkIU' {MYSQL_DB} "
    f"--skip-column-names "
    f"-e 'SELECT username,email,is_superuser,is_staff FROM accounts_user WHERE is_staff=TRUE OR is_superuser=TRUE LIMIT 20;' "
    f"2>/dev/null"
)
out3, err3 = ssh_exec(client, cmd3)
if out3:
    print('   username | email | superuser | staff')
    for line in out3.split('\n'):
        print(f'   {line}')
else:
    print(f'   err={err3[:300]}')

# 4. Total de usuários
print('\n[4] Total de usuários no banco:')
cmd4 = (
    f"docker exec {MYSQL_CONTAINER} mysql "
    f"-u{MYSQL_USER} -p'{MYSQL_PASS}' {MYSQL_DB} "
    f"--skip-column-names "
    f"-e 'SELECT COUNT(*),SUM(is_active),SUM(is_staff),SUM(is_superuser) FROM accounts_user;' "
    f"2>/dev/null"
)
out4, _ = ssh_exec(client, cmd4)
print(f'   total | ativos | staff | superuser')
print(f'   {out4}')

client.close()
print('\n   SSH encerrado.')

# 5. Testar health endpoint
print('\n[5] Health check público:')
status, body = http_get(f'{BASE_URL}/health/')
print(f'   GET /health/ → {status}')
if status == 200:
    try:
        data = json.loads(body)
        print(f'   {data}')
    except Exception:
        print(f'   {body[:100]}')

# 6. Tentar login com endpoint correto: /api/v1/auth/login/
print('\n[6] Testando login em /api/v1/auth/login/ ...')
test_users = [
    {'login': 'ghitflux@gmail.com', 'password': 'Admin@123'},
    {'login': 'ghitflux@gmail.com', 'password': 'admin'},
    {'login': 'helcio@abase.local', 'password': 'admin'},
    {'login': 'admin@abase.local', 'password': 'admin'},
    {'email': 'ghitflux@gmail.com', 'password': 'Admin@123'},
]
token = None
logged_user = None
for creds in test_users:
    status, body = http_post(f'{BASE_URL}/auth/login/', creds)
    label = creds.get('login') or creds.get('email', '?')
    print(f'   {label} → {status}')
    if status == 200:
        data = json.loads(body)
        token = data.get('access') or data.get('token') or data.get('access_token')
        logged_user = creds['username']
        print(f'   Token: {token[:50]}...')
        break
    elif status in (400, 401):
        try:
            err_data = json.loads(body)
            print(f'   Erro: {err_data}')
        except Exception:
            print(f'   Body: {body[:100]}')
    else:
        print(f'   Body: {body[:100]}')

if not token:
    print('\n   Nenhuma senha padrão funcionou.')
    print('   Para resetar: docker exec abase-backend-prod python manage.py changepassword <username>')

if token:
    print(f'\n[7] Testando endpoints autenticados (user={logged_user}):')
    endpoints = [
        '/auth/me/',
        '/associados/?page_size=1',
        '/contratos/?page_size=1',
        '/tesouraria/caixa/?page_size=1',
    ]
    for ep in endpoints:
        status, body = http_get(f'{BASE_URL}{ep}', token=token)
        print(f'   GET {ep} → {status}')
        if status == 200:
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    count = data.get('count', data.get('id', 'ok'))
                    print(f'   count/id={count}')
                else:
                    print(f'   ok')
            except Exception:
                print(f'   {body[:80]}')
        else:
            print(f'   {body[:150]}')

print('\n' + '=' * 60)
print('FIM DO TESTE')
print('=' * 60)
