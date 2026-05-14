# Procedimento de Atualização — Servidor Backup (abasepiaui.cloud)

## Visão Geral

| Item | Servidor Oficial | Servidor Backup |
|------|-----------------|-----------------|
| IP | 72.60.48.163 | 72.60.58.181 |
| Domínio | abasepiaui.com | abasepiaui.cloud |
| Usuário SSH | deploy (chave SSH) | root (senha) |
| Branch git | abaseprod | abase-prod-bckp |
| Senha SSH | — | `Xdxdapenas12345@` |

## O que é idêntico entre os servidores

- Código-fonte (mesma branch base, cherry-pick de abaseprod → abase-prod-bckp)
- Credenciais MySQL (`MYSQL_PASSWORD`, `SECRET_KEY`)
- Dump do banco de dados (copiado do oficial via `sync_db_from_prod.py`)

## O que difere no backup

- `DOMAIN`, `NEXT_PUBLIC_API_URL`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`: usa `abasepiaui.cloud`
- `nginx.conf`: `server_name` e caminhos SSL usam `abasepiaui.cloud`
- Branch git: `abase-prod-bckp`

---

## Passo a Passo — Atualização Completa

### 1. Sincronizar código

```bash
# No servidor backup (72.60.58.181):
cd /opt/ABASE/repo
git fetch origin
git pull --ff-only origin abase-prod-bckp
```

Após cada push para `abaseprod` (servidor oficial), fazer cherry-pick dos commits relevantes para `abase-prod-bckp` e fazer push.

### 2. Sincronizar env do servidor oficial

O arquivo `.env.production` deve ser copiado do servidor oficial e adaptado para o domínio `.cloud`:

- `NEXT_PUBLIC_API_URL=https://abasepiaui.cloud/api/v1`
- `DOMAIN=abasepiaui.cloud`
- `CORS_ALLOWED_ORIGINS=https://abasepiaui.cloud`
- `CSRF_TRUSTED_ORIGINS=https://abasepiaui.cloud`
- `ALLOWED_HOSTS=abasepiaui.com,abasepiaui.cloud,localhost,127.0.0.1`
- MySQL credentials: **manter idênticos ao oficial** (não alterar)

### 3. Adaptar nginx.conf

O `nginx.conf` em `/opt/ABASE/repo/deploy/hostinger/nginx/nginx.conf` deve usar `abasepiaui.cloud`:

```
server_name abasepiaui.cloud www.abasepiaui.cloud;
ssl_certificate /etc/letsencrypt/live/abasepiaui.cloud/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/abasepiaui.cloud/privkey.pem;
ssl_trusted_certificate /etc/letsencrypt/live/abasepiaui.cloud/chain.pem;
```

### 4. Rebuild completo (sem cache)

```bash
COMPOSE="docker compose -p abase --env-file /opt/ABASE/env/.env.production -f /opt/ABASE/repo/deploy/hostinger/docker-compose.prod.yml"

# Backend + Celery
nohup $COMPOSE build --no-cache backend celery > /tmp/build_backend.log 2>&1 &

# Aguardar conclusão (~5min)
tail -f /tmp/build_backend.log

# Frontend
nohup $COMPOSE build --no-cache frontend > /tmp/build_frontend.log 2>&1 &

# Aguardar conclusão (~5min)
tail -f /tmp/build_frontend.log
```

### 5. Recriar containers

```bash
$COMPOSE up -d --force-recreate --no-deps backend celery
sleep 8
$COMPOSE up -d --force-recreate --no-deps frontend
```

### 6. Reload nginx (OBRIGATÓRIO após recreate)

> **IMPORTANTE**: após recriar qualquer container, o nginx mantém o IP antigo em cache.
> Se não fizer reload, as rotas `/api/v1/` vão retornar 502 Bad Gateway.

```bash
docker exec abase-nginx-prod nginx -s reload
```

### 7. Verificar

```bash
# Status dos containers
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep abase

# Health check
curl -skL https://abasepiaui.cloud/api/v1/health -w " HTTP:%{http_code}"
# Esperado: {"status": "ok", "service": "abase-backend"} HTTP:200

# Frontend
curl -skL https://abasepiaui.cloud/ -o /dev/null -w "HTTP:%{http_code}"
# Esperado: HTTP:200
```

---

## Script Automatizado

Use o script `scripts/rebuild_backup_frontend.py` para executar os passos 4, 5 e 6 de forma automatizada:

```bash
python scripts/rebuild_backup_frontend.py
```

O script já inclui:
- Git pull da branch `abase-prod-bckp`
- Adaptação do `nginx.conf` para `abasepiaui.cloud`
- Build backend + celery (background, aguarda conclusão)
- Build frontend (background, aguarda conclusão)
- Recreate dos containers
- Reload do nginx
- Verificação do código novo e health check

---

## Sincronizar Banco de Dados

Para copiar o banco do servidor oficial para o backup:

```bash
python scripts/sync_db_from_prod.py
```

> **NÃO resetar senhas manualmente** — o dump do banco já traz as senhas hasheadas de produção.

---

## Troubleshooting

### 502 Bad Gateway após recreate de container

O nginx mantém o IP antigo do container em cache. Sempre executar após qualquer `--force-recreate`:

```bash
docker exec abase-nginx-prod nginx -s reload
```

### Backend em Restarting

Verificar se as credenciais MySQL estão corretas:

```bash
docker logs abase-backend-prod --tail 30
```

Se o erro for de autenticação MySQL, sincronizar o env do servidor oficial (o `MYSQL_PASSWORD` deve ser idêntico).

### Frontend com domínio errado (abasepiaui.com no lugar de abasepiaui.cloud)

O `NEXT_PUBLIC_API_URL` é baked no build. Verificar se `.env.production` no servidor backup tem:
```
NEXT_PUBLIC_API_URL=https://abasepiaui.cloud/api/v1
```
Se não, corrigir e fazer rebuild do frontend com `--no-cache`.

### Falha no build do frontend (pnpm ERR_PNPM_NO_MATCHING_VERSION)

O `Dockerfile.frontend` deve copiar o `pnpm-lock.yaml`. Verificar se:
```dockerfile
COPY pnpm-workspace.yaml package.json pnpm-lock.yaml ./
```
está presente na seção deps do `deploy/hostinger/Dockerfile.frontend`.

---

## Comandos Úteis

```bash
# Ver status de todos containers
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep abase

# Logs do backend
docker logs abase-backend-prod --tail 30 2>&1

# Logs do frontend
docker logs abase-frontend-prod --tail 20 2>&1

# Verificar código novo no frontend (novo design tem "Unindo")
docker exec abase-frontend-prod grep -r "Unindo" apps/web/.next/server/ 2>/dev/null | head -1

# Testar login
curl -sk https://abasepiaui.cloud/api/v1/auth/login/ \
  -X POST -H "Content-Type: application/json" \
  -d '{"login":"email@dominio.com","password":"senha"}'
```
