# Procedimento: Atualização do Servidor Backup

**Data de criação:** 2026-05-14  
**Servidor backup:** `abasepiaui.cloud` (`72.60.58.181`)  
**Servidor oficial:** `abasepiaui.com` (`72.60.48.163`)

---

## Visão Geral

O servidor backup é um espelho do servidor oficial. Toda atualização do servidor oficial
deve ser replicada para o backup usando o script `scripts/update_backup_server.py`.

### Diferenças entre os servidores

| Item | Servidor Oficial | Servidor Backup |
|------|-----------------|-----------------|
| Domínio | `abasepiaui.com` | `abasepiaui.cloud` |
| IP | `72.60.48.163` | `72.60.58.181` |
| SSH user | `deploy` | `root` |
| SSH auth | Chave `~/.ssh/abase_deploy` | Senha `Xdxdapenas12345@` |
| Git branch | `abaseprod` | `abase-prod-bckp` |
| Banco | produção real | cópia restaurada de produção |

### O que o backup compartilha com o oficial (IDÊNTICO)
- Código-fonte (mesmo commit, branch diferente)
- `SECRET_KEY`, credenciais MySQL, JWT, Redis
- Senhas dos usuários web (vêm do dump do banco de produção)

### O que o backup adapta (DIFERENTE)
- `DOMAIN=abasepiaui.cloud`
- `NEXT_PUBLIC_API_URL=https://abasepiaui.cloud/api/v1`
- `CORS_ALLOWED_ORIGINS` e `CSRF_TRUSTED_ORIGINS` com domínio `.cloud`
- `ALLOWED_HOSTS` inclui ambos `.com` e `.cloud` (para nginx)

---

## Quando atualizar o servidor backup

- Após qualquer deploy no servidor oficial
- Após restaurar um novo dump do banco de produção
- Nunca antes: o banco deve ser restaurado primeiro, depois o código

---

## Passo a Passo: Atualização Normal (código + env)

Executar a partir da máquina local com Python + Paramiko:

```bash
set PYTHONIOENCODING=utf-8
python scripts/update_backup_server.py
```

O script faz automaticamente:
1. Lê `.env.production` do servidor oficial (SSH + chave)
2. Adapta domínios: `abasepiaui.com` → `abasepiaui.cloud`
3. Grava o env no servidor backup via SFTP
4. Verifica git status (aborta se sujo)
5. `git fetch origin && git checkout abase-prod-bckp && git pull --ff-only`
6. `docker compose build --no-cache backend celery`
7. `docker compose build --no-cache frontend`
8. `up -d --force-recreate backend celery frontend`
9. `migrate --noinput`
10. `manage.py check`

**Tempo estimado:** ~5–10 minutos (build backend ~3min, frontend ~2min)

---

## Passo a Passo: Atualização com Novo Dump de Banco

Use quando quiser sincronizar os dados do banco de produção para o backup.

### 1. Fazer backup no servidor oficial

```bash
# (via script Paramiko no servidor oficial)
bash /opt/ABASE/repo/deploy/hostinger/scripts/backup_now.sh
```

### 2. Baixar dump para a máquina local

```bash
# O dump fica em /opt/ABASE/data/backups/ no servidor oficial
scp -i ~/.ssh/abase_deploy deploy@72.60.48.163:/opt/ABASE/data/backups/abase_YYYYMMDD_HHMMSS.sql.gz ./
```

### 3. Transferir dump para o servidor backup e restaurar

```bash
# Copiar para o backup
scp abase_YYYYMMDD_HHMMSS.sql.gz root@72.60.58.181:/tmp/

# No servidor backup (via SSH):
gunzip /tmp/abase_YYYYMMDD_HHMMSS.sql.gz
docker exec -i abase-mysql-prod mysql \
  -uroot -p'WkjZ40M9T@Vi9hj*Ddq26elZ4@HY' \
  --init-command="SET FOREIGN_KEY_CHECKS=0" \
  abase_v2 < /tmp/abase_YYYYMMDD_HHMMSS.sql
rm /tmp/abase_YYYYMMDD_HHMMSS.sql
```

> **IMPORTANTE:** As senhas de usuários vêm do dump — **não resetar** senhas
> manualmente. Os usuários do backup terão as mesmas senhas que na produção.

### 4. Atualizar código + env

```bash
python scripts/update_backup_server.py
```

---

## Credenciais do Servidor Backup

### Acesso SSH
```
Host: 72.60.58.181
Usuário: root
Senha: Xdxdapenas12345@
```

### MySQL (sincronizado com oficial)
```
Root password:  WkjZ40M9T@Vi9hj*Ddq26elZ4@HY
User/DB:        abase / abase_v2
User password:  eaSrW4*p6V%I26zoAQlLGSYPOzSK
```

### Django admin (após restauração do dump)
As credenciais são as mesmas do servidor de produção.  
O superuser default é `admin@abase.com` (id=1).

---

## Estrutura no Servidor Backup

```
/opt/ABASE/
├── repo/               # Código-fonte (branch abase-prod-bckp)
│   └── deploy/hostinger/docker-compose.prod.yml
├── env/
│   └── .env.production  # Env com domínios ajustados para .cloud
└── data/
    └── backups/         # Dumps do banco (se houver)
```

---

## Troubleshooting

### Backend em Restarting após update do env

Causa: credenciais MySQL no novo env não correspondem ao MySQL já inicializado.

Solução: atualizar senha do usuário `abase` no MySQL do backup para bater com o novo env:

```bash
# Conectar no servidor backup
ssh root@72.60.58.181

# Alterar senha do usuário abase
docker exec abase-mysql-prod mysql \
  -uroot -p'<SENHA_ROOT_ANTIGA>' \
  -e "ALTER USER 'abase'@'%' IDENTIFIED BY '<NOVA_SENHA>'; FLUSH PRIVILEGES;"

# Recriar backend
docker compose -p abase \
  --env-file /opt/ABASE/env/.env.production \
  -f /opt/ABASE/repo/deploy/hostinger/docker-compose.prod.yml \
  up -d --force-recreate --no-deps backend celery
```

Ou use o script auxiliar:
```bash
python scripts/fix_backup_mysql_credentials.py
```

### Frontend apontando para servidor errado

Verificar `NEXT_PUBLIC_API_URL` no env:
```bash
ssh root@72.60.58.181 "grep NEXT_PUBLIC_API_URL /opt/ABASE/env/.env.production"
# Deve retornar: NEXT_PUBLIC_API_URL=https://abasepiaui.cloud/api/v1
```

Se apontar para `abasepiaui.com`, executar:
```bash
python scripts/fix_backup_frontend_url.py
```

### Verificar saúde dos containers

```bash
ssh root@72.60.58.181 \
  "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep abase"
```

Todos devem mostrar `(healthy)`.

### Testar login no servidor backup

```bash
curl -s -X POST https://abasepiaui.cloud/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"login":"admin@abase.com","password":"<senha_producao>"}'
```

---

## Relação entre Branches e Servidores

```
Local (abaseprod)
    │
    ├── git push origin abaseprod       → GitHub abasev2.git
    └── git push abasenewv2 abaseprod   → GitHub abasenewv2.git (usado pelo servidor oficial)

abase-prod-bckp (local) = mesmos commits que abaseprod
    │
    └── git push origin abase-prod-bckp → GitHub abasenewv2.git (usado pelo servidor backup)

Servidor oficial (72.60.48.163):
    origin → abasenewv2.git
    branch → abaseprod

Servidor backup (72.60.58.181):
    origin → abasenewv2.git
    branch → abase-prod-bckp
```

Para manter `abase-prod-bckp` sincronizado com `abaseprod` localmente:
```bash
git checkout abase-prod-bckp
git merge --ff-only abaseprod
git push origin abase-prod-bckp
git push abasenewv2 abase-prod-bckp
```

---

## Scripts relacionados

| Script | Uso |
|--------|-----|
| `scripts/update_backup_server.py` | **Principal** — atualiza código + env |
| `scripts/fix_backup_mysql_credentials.py` | Corrige senha MySQL após troca de env |
| `scripts/fix_backup_frontend_url.py` | Corrige domínio no frontend |
| `scripts/deploy_production.py` | Deploy no servidor oficial |
| `docs/PADRAO_OPERACIONAL_PARAMIKO_SERVIDOR.md` | Padrão geral de deploy Paramiko |
