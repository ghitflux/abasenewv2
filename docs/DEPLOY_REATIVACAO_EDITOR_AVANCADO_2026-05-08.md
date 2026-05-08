# Deploy 2026-05-08 - reativacao com evidencia historica e editor avancado

## Objetivo

Subir o pacote que corrige dois bloqueios operacionais identificados no caso
`MARIA LUSINETE SILVA SANTOS`, CPF `286.755.683-04`:

- em `Tesouraria > Novos Contratos > Reativacoes`, a efetivacao nao deve falhar
  quando uma competencia do novo ciclo ja possui evidencia financeira no
  historico;
- no detalhe do associado, o `Editor avancado` nao deve bloquear o `save-all`
  com `Parcela sem ciclo de destino valido` quando a parcela existente pode ser
  associada com seguranca ao ciclo atual do proprio contrato.
- no `Editar cadastro` aberto pelo modo editor avancado, o admin/coordenador
  deve conseguir alterar o status do associado e o status do contrato
  operacional, incluindo colocar ambos como `Ativo`.

Nao ha migration neste pacote.
Nao ha comando de reparo obrigatorio para rodar no servidor.

## Regras corrigidas

- Parcela historica com evidencia financeira real fica preservada.
- Competencia historica com evidencia deixa de travar a efetivacao da
  reativacao.
- Parcela conflitante sem evidencia continua sendo cancelada/ocultada para
  liberar a materializacao da reativacao.
- Novo ciclo da reativacao continua sendo criado com as competencias
  confirmadas pela tesouraria.
- Upload de comprovantes permanece aditivo: nova versao entra no historico e
  nao apaga versoes anteriores.
- Interface da tesouraria passa a exibir `Adicionar versao` quando ja existe
  comprovante.
- `save-all` do editor avancado recupera `cycle_ref` vazio quando a parcela
  existente ja pertence a um ciclo valido do contrato.
- Erro `Parcela sem ciclo de destino valido` permanece para referencia
  realmente inexistente ou irrecuperavel.
- Formulario administrativo de cadastro passa a expor `Status do associado` e
  `Status do contrato`.
- Contrato operacional marcado como `ativo` sem ciclo materializado deixa de
  aparecer visualmente como `Em Analise` e passa a aparecer como `Ativo`.

## Arquivos do pacote

- `backend/apps/tesouraria/services.py`
- `backend/apps/associados/admin_override_service.py`
- `backend/apps/contratos/cycle_projection.py`
- `backend/apps/associados/tests/test_reactivation.py`
- `backend/apps/associados/tests/test_admin_overrides.py`
- `apps/web/src/components/associados/associado-form.tsx`
- `apps/web/src/app/(dashboard)/tesouraria/page.tsx`
- `apps/web/src/app/(dashboard)/tesouraria/page.test.tsx`

## Validacao local executada

```bash
docker compose run --rm backend-tools python manage.py test \
  apps.associados.tests.test_reactivation.AssociadoReactivationTestCase.test_efetivacao_reativacao_preserva_competencia_com_evidencia_historica \
  apps.associados.tests.test_reactivation.AssociadoReactivationTestCase.test_efetivacao_reativacao_cria_ciclo_abril_maio_junho \
  apps.associados.tests.test_admin_overrides.AdminOverrideApiTestCase.test_save_all_resolve_parcela_sem_cycle_ref_pelo_ciclo_atual \
  apps.associados.tests.test_admin_overrides.AdminOverrideApiTestCase.test_save_all_returns_validation_message_for_invalid_cycle_reference \
  --settings=config.settings.testing --noinput
```

Resultado: `4 tests OK`.

```bash
docker compose run --rm backend-tools python manage.py test \
  apps.tesouraria.tests.test_fluxo_completo.TestFluxoCompleto.test_tesouraria_serializer_expoe_evidencias_canonicas_nos_comprovantes \
  apps.tesouraria.tests.test_fluxo_completo.TestFluxoCompleto.test_tesouraria_pode_devolver_reativacao_para_analise \
  apps.tesouraria.tests.test_fluxo_completo.TestFluxoCompleto.test_remover_reativacao_antiga_nao_remove_reativacao_atual_da_tesouraria \
  --settings=config.settings.testing --noinput
```

Resultado: `3 tests OK`.

```bash
docker compose run --rm backend-tools python manage.py test \
  apps.associados.tests.test_reactivation \
  --settings=config.settings.testing --noinput
```

Resultado: `9 tests OK`.

```bash
docker compose run --rm backend-tools python manage.py test \
  apps.associados.tests.test_admin_overrides.AdminOverrideApiTestCase.test_admin_can_set_associado_and_contract_status_to_active \
  apps.associados.tests.test_admin_overrides.AdminOverrideApiTestCase.test_admin_can_override_associado_and_contract_core_with_audit \
  --settings=config.settings.testing --noinput
```

Resultado: `2 tests OK`.

```bash
docker compose run --rm frontend pnpm --filter @abase/web exec jest \
  --runInBand --runTestsByPath \
  src/app/\(dashboard\)/tesouraria/page.test.tsx
```

Resultado: `1 test suite passed`, `8 tests passed`.

```bash
docker compose run --rm frontend pnpm --filter @abase/web type-check
```

Resultado: sem erro.

```bash
git diff --check
```

Resultado: sem erro.

### Observacao sobre suite ampla

Tambem foi executada a suite completa:

```bash
docker compose run --rm backend-tools python manage.py test \
  apps.associados.tests.test_admin_overrides \
  --settings=config.settings.testing --noinput
```

Resultado: `40` testes passaram e `2` falharam. As duas falhas tambem falham
quando executadas isoladamente e nao passam pelos trechos alterados neste
pacote:

- `test_refinanciamento_core_override_syncs_associado_status_after_desativacao`
- `test_save_all_manual_layout_does_not_materialize_renewal_queue_on_rebuild`

Essas falhas devem ser tratadas em pacote separado, para nao misturar regra de
reativacao com ajuste de renovacao/editor ja existente.

## Deploy via Paramiko

Use o padrao permanente de operacao remota:

- `docs/PADRAO_OPERACIONAL_PARAMIKO_SERVIDOR.md`

Execute a partir da maquina local de deploy.

```bash
python -m pip install paramiko

export ABASE_HOST="IP_OU_HOST_DO_SERVIDOR"
export ABASE_USER="USUARIO_SSH"
export ABASE_KEY="/caminho/para/chave_ssh"
export ABASE_BRANCH="abaseprod"
```

```bash
python - <<'PY'
import os
import sys
import paramiko

host = os.environ["ABASE_HOST"]
user = os.environ["ABASE_USER"]
key_path = os.environ["ABASE_KEY"]
branch = os.environ.get("ABASE_BRANCH", "abaseprod")

compose = (
    "docker compose -p abase "
    "--env-file /opt/ABASE/env/.env.production "
    "-f deploy/hostinger/docker-compose.prod.yml"
)

commands = [
    "cd /opt/ABASE/repo && git status --short",
    "cd /opt/ABASE/repo && git rev-parse HEAD",
    "cd /opt/ABASE/repo && bash deploy/hostinger/scripts/backup_now.sh",
    f"cd /opt/ABASE/repo && git fetch origin && git checkout {branch} && git pull --ff-only origin {branch}",
    "cd /opt/ABASE/repo && git rev-parse HEAD",
    f"cd /opt/ABASE/repo && {compose} build backend frontend",
    f"cd /opt/ABASE/repo && {compose} up -d --force-recreate --no-deps backend frontend",
    f"cd /opt/ABASE/repo && {compose} exec -T backend python manage.py migrate --noinput",
    f"cd /opt/ABASE/repo && {compose} exec -T backend python manage.py check",
    f"cd /opt/ABASE/repo && {compose} ps",
]

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(hostname=host, username=user, key_filename=key_path, timeout=30)

try:
    for index, command in enumerate(commands):
        print(f"\n$ {command}", flush=True)
        stdin, stdout, stderr = client.exec_command(command, get_pty=True)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
        if out:
            print(out, end="")
        if err:
            print(err, end="", file=sys.stderr)
        if status != 0:
            raise SystemExit(f"Comando falhou com status {status}: {command}")
        if index == 0 and out.strip():
            raise SystemExit(
                "Servidor possui alteracoes locais. Interrompa o deploy e salve o diff."
            )
finally:
    client.close()
PY
```

## Validacao no servidor apos deploy

### Health tecnica

```bash
cd /opt/ABASE/repo
docker compose -p abase --env-file /opt/ABASE/env/.env.production \
  -f deploy/hostinger/docker-compose.prod.yml ps
docker compose -p abase --env-file /opt/ABASE/env/.env.production \
  -f deploy/hostinger/docker-compose.prod.yml exec -T backend python manage.py check
```

Esperado:

- `backend` e `frontend` em execucao;
- `manage.py check` sem issues.

### Validacao operacional - Maria Lusinete

1. Abrir `/tesouraria`.
2. Entrar em `Novos Contratos`.
3. Filtrar ou localizar `MARIA LUSINETE SILVA SANTOS`, CPF
   `286.755.683-04`.
4. Na secao `Reativacoes`, anexar comprovante do associado e do agente se ainda
   faltar alguma versao.
5. Confirmar que o botao mostra `Adicionar versao` quando ja existe arquivo.
6. Clicar em `Efetivar`.
7. Confirmar o ciclo sugerido.

Esperado:

- nao deve aparecer `A competencia 04/2026 ja possui evidencia financeira`;
- comprovantes antigos continuam visiveis no historico;
- reativacao efetiva com sucesso;
- novo ciclo da reativacao nasce com as competencias confirmadas;
- associado volta para status ativo quando a efetivacao conclui.

### Validacao operacional - editor avancado

1. Abrir o detalhe da associada.
2. Ativar `Modo editor avancado`.
3. Ajustar o ciclo/parcela necessario.
4. Clicar em `Salvar alteracoes` / `Salvar tudo`.

Esperado:

- nao deve aparecer `Parcela sem ciclo de destino valido` para parcela existente
  dentro de ciclo valido;
- o historico do associado registra o motivo informado;
- a tela recarrega os dados do editor sem erro generico.

### Validacao operacional - status do associado

1. Abrir o detalhe do associado que esta exibindo `Em Analise`.
2. Ativar `Modo editor avancado`.
3. Clicar em `Editar cadastro`.
4. Na primeira etapa do formulario, alterar `Status do associado` para `Ativo`.
5. Alterar `Status do contrato` para `Ativo`.
6. Informar o motivo administrativo, confirmar e salvar.
7. Voltar para o detalhe do associado.

Esperado:

- o payload deve chamar `admin-overrides/associados/{id}/core/`;
- o associado deve ficar com status `ativo`;
- o contrato operacional deve ficar com status `ativo`;
- o selo visual do topo deve deixar de mostrar `Em Analise` e passar a mostrar
  `Ativo`, salvo se outra regra operacional mais especifica estiver ativa
  para o associado.

## Rollback

Rollback padrao por SHA anterior validado:

```bash
export ABASE_ROLLBACK_SHA="SHA_ANTERIOR_VALIDADO"
```

```bash
python - <<'PY'
import os
import sys
import paramiko

host = os.environ["ABASE_HOST"]
user = os.environ["ABASE_USER"]
key_path = os.environ["ABASE_KEY"]
rollback_sha = os.environ["ABASE_ROLLBACK_SHA"]

compose = (
    "docker compose -p abase "
    "--env-file /opt/ABASE/env/.env.production "
    "-f deploy/hostinger/docker-compose.prod.yml"
)

commands = [
    "cd /opt/ABASE/repo && git status --short",
    "cd /opt/ABASE/repo && git rev-parse HEAD",
    f"cd /opt/ABASE/repo && git checkout {rollback_sha}",
    f"cd /opt/ABASE/repo && {compose} build backend frontend",
    f"cd /opt/ABASE/repo && {compose} up -d --force-recreate --no-deps backend frontend",
    f"cd /opt/ABASE/repo && {compose} exec -T backend python manage.py check",
    f"cd /opt/ABASE/repo && {compose} ps",
]

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(hostname=host, username=user, key_filename=key_path, timeout=30)

try:
    for index, command in enumerate(commands):
        print(f"\n$ {command}", flush=True)
        stdin, stdout, stderr = client.exec_command(command, get_pty=True)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
        if out:
            print(out, end="")
        if err:
            print(err, end="", file=sys.stderr)
        if status != 0:
            raise SystemExit(f"Comando falhou com status {status}: {command}")
        if index == 0 and out.strip():
            raise SystemExit(
                "Servidor possui alteracoes locais. Interrompa o rollback e salve o diff."
            )
finally:
    client.close()
PY
```

Depois do rollback, repetir as validacoes de health tecnica e registrar o SHA
restaurado.
