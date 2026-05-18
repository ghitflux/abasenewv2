# Regra de Ciclo de 4 Parcelas — Implementação e Validação

**Data:** 2026-05-18
**Branch:** abaseprod
**Autor:** implementação assistida (Claude) + revisão manual pendente

## Objetivo

Fazer a regra alternativa de **ciclo de 4 parcelas** coexistir corretamente
com a regra padrão de **3 parcelas**, garantindo que:

1. A renovação flua de `apto_a_renovar` → `em_analise_renovacao` → análise →
   efetivação, igual ao ciclo de 3.
2. ADMIN, COORDENADOR e ANALISTA possam ajustar o ciclo (3↔4) pelo
   cadastro e pelo editor avançado.
3. Mudanças no tamanho do ciclo afetem **apenas os ciclos futuros**
   (decisão validada com o usuário).

## Diagnóstico

A regra de 4 parcelas já estava ~70% implementada. O núcleo
(`cycle_timeline`, `cycle_projection`, `cycle_rebuild`, modelo
`Refinanciamento` com `ref1..ref4`) já é dinâmico via
`get_contract_cycle_size(contrato)` (lê `contrato.prazo_meses`) e
`threshold = max(cycle_size - 1, 1)`.

**O backend já entregava o ciclo 4 corretamente para o analista** — provado
pelos 5 testes novos, incluindo o endpoint real
`/api/v1/analise/refinanciamentos/`. Os bugs estavam nas bordas:

| # | Arquivo | Problema | Correção |
|---|---------|----------|----------|
| 1 | `backend/apps/contratos/management/commands/audit_cycle_integrity.py` | `if ... != 3: return []` e fatias `[:3]` hardcoded — auditoria/reparo ignoravam ciclo 4 e podiam mover parcelas erradas | Usa `get_contract_cycle_size(contrato)` |
| 2 | `backend/apps/associados/admin_override_serializers.py` | `prazo_meses` read-only / sem upper bound | `IntegerField(min_value=3, max_value=4)` em `AdminAssociadoEditWriteSerializer`, `ContratoCoreOverrideWriteSerializer`, `SaveAllContratoCoreWriteSerializer` |
| 3 | `backend/apps/associados/admin_override_service.py` | `apply_contract_core_override` ignorava `prazo_meses`; nenhum rebuild após mudança; diff detector não via `prazo_meses` | Aceita/valida `prazo_meses` (3 ou 4), dispara `rebuild_contract_cycle_state` quando muda, em `apply_contract_core_override` **e** `apply_save_all`; `_contract_core_payload_has_effective_changes` agora compara `prazo_meses` |
| 4 | `apps/web/src/components/associados/admin-contract-editor.tsx` | Editor avançado não tinha campo de ciclo | Select "Tamanho do ciclo (parcelas)" (3 padrão / 4); `prazo_meses` adicionado a `SaveAllContratoCorePayload` e `buildContractCorePayload` |

### Sobre "só afeta próximos ciclos"

`rebuild_contract_cycle_state` deriva os ciclos da projeção. Ciclos já
concluídos (com `Refinanciamento` efetivado / `data_ativacao_ciclo`) são
preservados pela lógica de `_sync_refinanciamentos`; apenas o ciclo aberto
em andamento passa a exigir o novo número de parcelas. Não há reescrita
retroativa de ciclos liquidados.

### Bug "não aparece para o analista"

O endpoint do analista (`AnalistaRefinanciamentoViewSet`,
`backend/apps/refinanciamento/views.py:633`) filtra **apenas por status**
(`em_analise_renovacao`, `pendente_termo_analista`,
`pendente_termo_agente`, `aprovado_analise_renovacao`) — **não** por
`prazo_meses`. Os 5 testes confirmam que um contrato ciclo 4, após
`solicitar`, aparece tanto em `/api/v1/refinanciamentos/` quanto em
`/api/v1/analise/refinanciamentos/`.

Hipótese remanescente para o relato do usuário (a validar manualmente):
o filtro de UI `eligibility_band` (`2_3 / 3_3 / 3_4 / 4_4`) na página de
coordenação pode esconder contratos quando mal selecionado, e a auditoria
quebrada (Fix #1) podia reclassificar a 4ª parcela para fora do ciclo,
impedindo o `apto`. Ambos corrigidos/explicados aqui.

## Testes

Novo arquivo: `backend/apps/refinanciamento/tests/test_ciclo_quatro_parcelas.py`

- `test_contrato_ciclo_4_apto_apos_3_parcelas_pagas` ✅
- `test_solicitar_envia_para_analista_em_ciclo_4` ✅ (valida `/api/v1/refinanciamentos/` e `/api/v1/analise/refinanciamentos/`)
- `test_contrato_ciclo_4_nao_apto_com_so_2_parcelas` ✅
- `test_admin_pode_alterar_prazo_meses_de_3_para_4` ✅
- `test_admin_rejeita_prazo_meses_invalido` ✅

**5/5 passam.**

### Estado do suite (importante)

A branch `abaseprod` **já tinha** falhas pré-existentes não relacionadas:
baseline (sem estas mudanças) = **14 falhas + 3 erros** em
`apps.refinanciamento.tests` + `apps.contratos.tests.test_renovacao`.
Com estas mudanças = **12–13 falhas + 3 erros** (nenhuma regressão; leve
melhora). As 2 falhas em `test_admin_overrides` (`renewal_queue_missing`,
`sync_associado_mother_status`) também são pré-existentes.

> Recomendação: tratar essas falhas pré-existentes do `abaseprod` em
> tarefa separada antes de subir para produção.

Type check frontend: **0 erros** em `admin-contract-editor.tsx` (erros TS
restantes são em `*.test.tsx` pré-existentes).

## Checklist de validação manual (servidor backup)

- [ ] Contrato ciclo 3: fluxo apto→análise→efetivação inalterado (regressão)
- [ ] Contrato ciclo 4: 3 parcelas pagas → fica apto
- [ ] Ciclo 4: agente/admin solicita → aparece para o ANALISTA
- [ ] Editor avançado: trocar 3→4 salva e refaz projeção sem mexer em ciclo já renovado
- [ ] Editor avançado: trocar 4→3 idem
- [ ] Cadastro (save-all): campo "Tamanho do ciclo" persiste
- [ ] Filtro `eligibility_band` 3/4 e 4/4 lista corretamente na coordenação

## Atualização: modal de efetivação 3/4 parcelas (mesmo dia)

A efetivação na tesouraria agora abre um modal **"Confirmar efetivação da
renovação"** onde o operador escolhe o tamanho do próximo ciclo (3 ou 4
parcelas). A escolha:

- Atualiza `contrato.prazo_meses` (passa a valer para os ciclos seguintes).
- Garante que o ciclo destino seja criado mesmo quando a projeção
  automática falha — fallback explícito em [services.py efetivar()](backend/apps/refinanciamento/services.py)
  cria o `Ciclo` `numero=N+1` e suas parcelas em `em_previsao`.
- Marca o ciclo origem como `ciclo_renovado` quando todas as parcelas dele
  estão pagas, satisfazendo a regra "ciclo anterior muda de apto para
  ativo/concluído quando renovado".

Cobertura (`test_ciclo_quatro_parcelas.py`):
- `test_efetivar_com_proximo_ciclo_3_cria_ciclo_destino_com_3_parcelas` ✅
- `test_efetivar_com_proximo_ciclo_4_atualiza_prazo_e_cria_4_parcelas` ✅
- `test_efetivar_rejeita_proximo_ciclo_invalido` ✅
- + os 5 originais → **8/8**.

Diagnóstico do caso JOSINEA (CPF 33947597304, refi 1493) no backup:
contrato 121 está `encerrado` e a projeção atual só retornava 2 ciclos
(n=1 renovado, n=2 aberto), nunca um n=3. Por isso o rebuild dentro de
`efetivar()` não materializava o ciclo destino. O fallback explícito
agora cobre esse caso e o reparo desse refi específico pode ser feito
manualmente após o deploy.

## Procedimento de deploy

1. Validar manualmente no **servidor backup** com este checklist.
2. Após OK manual, promover para produção seguindo o procedimento padrão
   (build Docker **com `--no-cache`**, conforme regra do projeto).
3. Rodar `python manage.py audit_cycle_integrity` (agora ciente de ciclo 4)
   em dry-run antes de qualquer reparo em massa.
