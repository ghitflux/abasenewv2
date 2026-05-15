"""Simulação read-only do arquivo retorno (dry-run).

Executa toda a lógica de reconciliação SEM escrever no banco, retornando
um dict estruturado com KPIs e items prontos para armazenar no JSONField
`dry_run_resultado` do modelo ArquivoRetorno.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from apps.associados.models import Associado, only_digits
from apps.contratos.competencia import resolve_processing_competencia_parcela
from apps.contratos.cycle_projection import (
    ACTIVE_OPERATIONAL_REFINANCIAMENTO_STATUSES,
    build_contract_cycle_projection,
    is_contract_eligible_for_renewal_competencia,
)
from apps.contratos.models import Contrato, Parcela

from .matching import find_associado
from .models import ArquivoRetorno
from .return_auto_enrollment import (
    is_synthetic_return_contract_code,
    should_align_parcela_value_from_return,
)

VALORES_3050 = {Decimal("30.00"), Decimal("50.00")}
MENSALIDADE_MIN = Decimal("100.00")
DRY_RUN_BLOCKING_RENEWAL_STATUSES = (
    ACTIVE_OPERATIONAL_REFINANCIAMENTO_STATUSES - {"apto_a_renovar"}
)


def _parse_competencia(value: str) -> date:
    return datetime.strptime(value, "%m/%Y").date().replace(day=1)


def _next_month(value: date) -> date:
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    return date(year, month, 1)


def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _categorizar_valor(valor) -> str:
    v = _to_decimal(valor)
    if v in VALORES_3050:
        return "valores_30_50"
    if v >= MENSALIDADE_MIN:
        return "mensalidades"
    return "outros"


def _determine_resultado(raw: dict, parcela: Parcela) -> str:
    cod = raw.get("status_codigo", "")
    valor = _to_decimal(raw.get("valor_descontado", "0"))

    if cod == "1":
        if parcela.status == Parcela.Status.LIQUIDADA:
            return "pendencia_manual"
        if valor != parcela.valor and not should_align_parcela_value_from_return(
            parcela=parcela,
            return_value=valor,
        ):
            return "pendencia_manual"
        return "baixa_efetuada"

    if cod == "4":
        # permitir_diferenca=True — divergência sinalizada mas efetivado
        return "baixa_efetuada"

    if cod in ("2", "3", "S"):
        return "nao_descontado"

    if cod in ("5", "6"):
        return "pendencia_manual"

    return "erro"


def _simular_status_associado(status_atual: str | None, resultado: str) -> str | None:
    if resultado == "nao_descontado":
        return Associado.Status.INADIMPLENTE
    if resultado == "baixa_efetuada" and status_atual in {
        Associado.Status.IMPORTADO,
        Associado.Status.CADASTRADO,
        Associado.Status.EM_ANALISE,
        Associado.Status.PENDENTE,
        Associado.Status.INADIMPLENTE,
    }:
        return Associado.Status.ATIVO
    return status_atual


def _simular_status_ciclo(status_atual: str | None, resultado: str, apto: bool) -> str | None:
    del resultado
    if apto:
        return "apto_a_renovar"
    return status_atual


def _simular_item(raw: dict, competencia: date) -> dict:
    cpf = only_digits(raw.get("cpf_cnpj", ""))
    status_codigo = raw.get("status_codigo", "")
    associado_importado = False

    base = {
        "linha_numero": raw.get("linha_numero"),
        "cpf_cnpj": cpf,
        "nome_servidor": raw.get("nome_servidor", ""),
        "matricula_servidor": raw.get("matricula_servidor", ""),
        "orgao_pagto_nome": raw.get("orgao_pagto_nome", ""),
        "valor_descontado": str(_to_decimal(raw.get("valor_descontado", "0"))),
        "status_codigo": status_codigo,
        "categoria": _categorizar_valor(raw.get("valor_descontado")),
    }

    associado = find_associado(
        cpf=cpf,
        matricula=raw.get("matricula_servidor"),
        nome=raw.get("nome_servidor"),
        orgao=raw.get("orgao_pagto_nome"),
        orgao_alternativo=raw.get("orgao_pagto_codigo"),
        orgao_codigo=raw.get("orgao_codigo"),
    )

    if not associado:
        associado_importado = True
        resultado = _determine_resultado(
            raw,
            Parcela(
                numero=1,
                referencia_mes=competencia,
                valor=_to_decimal(raw.get("valor_descontado", "0")),
                data_vencimento=competencia,
                status=Parcela.Status.EM_ABERTO,
            ),
        )
        return {
            **base,
            "resultado": resultado,
            "associado_id": None,
            "associado_nome": raw.get("nome_servidor", ""),
            "associado_status_antes": None,
            "associado_status_depois": (
                Associado.Status.ATIVO
                if status_codigo in ("1", "4")
                else Associado.Status.INADIMPLENTE
                if status_codigo in ("2", "3", "S")
                else Associado.Status.IMPORTADO
            ),
            "ciclo_status_antes": None,
            "ciclo_status_depois": None,
            "ficara_apto_renovar": False,
            "desconto_em_associado_inativo": False,
            "associado_importado": associado_importado,
            "_associado_id": None,
            "_contrato_id": None,
            "_parcela_id": None,
            "_parcela_status_antes": None,
        }

    parcela = resolve_processing_competencia_parcela(
        associado_id=associado.id,
        referencia_mes=competencia,
        for_update=False,
    )
    if parcela is not None and is_synthetic_return_contract_code(
        getattr(parcela.ciclo.contrato, "codigo", "")
    ):
        parcela = None

    if not parcela:
        resultado = _determine_resultado(
            raw,
            Parcela(
                numero=1,
                referencia_mes=competencia,
                valor=_to_decimal(raw.get("valor_descontado", "0")),
                data_vencimento=competencia,
                status=Parcela.Status.EM_ABERTO,
            ),
        )
        return {
            **base,
            "resultado": resultado,
            "associado_id": associado.id,
            "associado_nome": associado.nome_completo,
            "associado_status_antes": associado.status,
            "associado_status_depois": _simular_status_associado(associado.status, resultado),
            "ciclo_status_antes": None,
            "ciclo_status_depois": None,
            "ficara_apto_renovar": False,
            "desconto_em_associado_inativo": (
                resultado == "baixa_efetuada" and associado.status == Associado.Status.INATIVO
            ),
            "associado_importado": False,
            "_associado_id": associado.id,
            "_contrato_id": None,
            "_parcela_id": None,
            "_parcela_status_antes": None,
        }

    resultado = _determine_resultado(raw, parcela)
    assoc_depois = _simular_status_associado(associado.status, resultado)
    ciclo_antes = parcela.ciclo.status

    return {
        **base,
        "resultado": resultado,
        "associado_id": associado.id,
        "associado_nome": associado.nome_completo,
        "associado_status_antes": associado.status,
        "associado_status_depois": assoc_depois,
        "ciclo_status_antes": ciclo_antes,
        "ciclo_status_depois": ciclo_antes,
        "ficara_apto_renovar": False,
        "desconto_em_associado_inativo": (
            resultado == "baixa_efetuada" and associado.status == Associado.Status.INATIVO
        ),
        "associado_importado": associado_importado,
        "_associado_id": associado.id,
        "_contrato_id": parcela.ciclo.contrato_id,
        "_parcela_id": parcela.id,
        "_parcela_status_antes": parcela.status,
    }


def _status_simulado_parcela(resultado: str, status_codigo: str, status_atual: str) -> str:
    if resultado == "baixa_efetuada" and status_codigo in {"1", "4"}:
        return Parcela.Status.DESCONTADO
    if resultado == "nao_descontado" and status_codigo in {"2", "3", "S"}:
        return Parcela.Status.NAO_DESCONTADO
    return status_atual


def _project_latest_cycle_parcelas(
    contrato: Contrato,
    *,
    projection: dict[str, object],
    simulated_status_by_parcela_id: dict[int, str],
) -> list[dict[str, object]]:
    cycles = list(
        sorted(
            projection.get("cycles") or [],
            key=lambda item: int(item.get("numero") or 0),
        )
    )
    if not cycles:
        return []

    simulated_ids = set(simulated_status_by_parcela_id)
    matched_projected_cycle = next(
        (
            cycle
            for cycle in reversed(cycles)
            if any(
                parcela.get("id") is not None
                and int(parcela["id"]) in simulated_ids
                for parcela in cycle.get("parcelas") or []
            )
        ),
        None,
    )
    latest_cycle = matched_projected_cycle or cycles[-1]
    if matched_projected_cycle is None and simulated_ids:
        physical_cycle = (
            contrato.ciclos.filter(parcelas__id__in=simulated_ids)
            .order_by("-numero", "-id")
            .first()
        )
        if physical_cycle is not None:
            return [
                {
                    "referencia_mes": parcela.referencia_mes,
                    "status": simulated_status_by_parcela_id.get(parcela.id, parcela.status),
                    "data_pagamento": parcela.data_pagamento,
                }
                for parcela in physical_cycle.parcelas.order_by("numero", "id")
            ]
    projected_parcelas = []
    for parcela in latest_cycle.get("parcelas") or []:
        parcela_id = parcela.get("id")
        status = parcela.get("status")
        if parcela_id is not None:
            status = simulated_status_by_parcela_id.get(int(parcela_id), str(status or ""))
        projected_parcelas.append(
            {
                "referencia_mes": parcela.get("referencia_mes"),
                "status": status,
                "data_pagamento": parcela.get("data_pagamento"),
            }
        )

    if projected_parcelas:
        return projected_parcelas

    latest_physical_cycle = contrato.ciclos.order_by("-numero", "-id").first()
    if latest_physical_cycle is None:
        return []

    return [
        {
            "referencia_mes": parcela.referencia_mes,
            "status": simulated_status_by_parcela_id.get(parcela.id, parcela.status),
            "data_pagamento": parcela.data_pagamento,
        }
        for parcela in latest_physical_cycle.parcelas.order_by("numero", "id")
    ]


def _apply_renewal_transition_flags(resultados: list[dict], competencia: date) -> None:
    contrato_ids = {
        int(row["_contrato_id"])
        for row in resultados
        if row.get("_contrato_id") is not None and row.get("_parcela_id") is not None
    }
    if not contrato_ids:
        return

    simulated_status_by_parcela_id = {
        int(row["_parcela_id"]): _status_simulado_parcela(
            str(row.get("resultado") or ""),
            str(row.get("status_codigo") or ""),
            str(row.get("_parcela_status_antes") or ""),
        )
        for row in resultados
        if row.get("_parcela_id") is not None
    }
    rows_by_contract: dict[int, list[dict]] = defaultdict(list)
    for row in resultados:
        if row.get("_contrato_id") is not None:
            rows_by_contract[int(row["_contrato_id"])].append(row)

    renovacao_competencia = _next_month(competencia)
    has_persisted_snapshot_for_competencia = ArquivoRetorno.objects.filter(
        competencia=competencia,
        status=ArquivoRetorno.Status.CONCLUIDO,
    ).exists()
    contratos = Contrato.objects.select_related("associado").filter(id__in=contrato_ids)
    marked_associados: set[int] = set()
    for contrato in contratos:
        associado = contrato.associado
        associado_id = contrato.associado_id
        if associado_id in marked_associados:
            continue

        # Fast pre-checks before the expensive build_contract_cycle_projection call.
        # 1) When a completed archive already exists for this competência, only
        #    contracts whose associado is already APTO_A_RENOVAR can be flagged.
        if has_persisted_snapshot_for_competencia and associado.status != Associado.Status.APTO_A_RENOVAR:
            continue

        # 2) Only contracts with at least one "baixa_efetuada mensalidade" row
        #    can become eligible for renewal — skip the expensive projection early.
        representative = next(
            (
                row
                for row in rows_by_contract.get(contrato.id, [])
                if row.get("resultado") == "baixa_efetuada"
            ),
            None,
        )
        if representative is None:
            continue
        if representative.get("categoria") != "mensalidades":
            continue

        projection = build_contract_cycle_projection(contrato)
        projected_parcelas = _project_latest_cycle_parcelas(
            contrato,
            projection=projection,
            simulated_status_by_parcela_id=simulated_status_by_parcela_id,
        )
        if not projected_parcelas:
            continue

        after_apto = is_contract_eligible_for_renewal_competencia(
            contrato,
            competencia=renovacao_competencia,
            parcelas=projected_parcelas,
            projection=projection,
        )
        if not after_apto:
            continue

        if not has_persisted_snapshot_for_competencia and contrato.refinanciamentos.filter(
            deleted_at__isnull=True,
            status__in=DRY_RUN_BLOCKING_RENEWAL_STATUSES,
        ).exists():
            continue

        representative["ficara_apto_renovar"] = True
        representative["ciclo_status_depois"] = _simular_status_ciclo(
            representative.get("ciclo_status_antes"),
            representative.get("resultado"),
            True,
        )
        marked_associados.add(associado_id)


def _strip_internal_fields(resultados: list[dict]) -> list[dict]:
    return [
        {
            key: value
            for key, value in row.items()
            if not key.startswith("_")
        }
        for row in resultados
    ]


def _build_kpis(resultados: list[dict]) -> dict:
    total = len(resultados)
    baixa = [r for r in resultados if r["resultado"] == "baixa_efetuada"]
    nao_desc = [r for r in resultados if r["resultado"] == "nao_descontado"]
    nao_enc = [r for r in resultados if r["resultado"] == "nao_encontrado"]
    pendencia = [r for r in resultados if r["resultado"] == "pendencia_manual"]
    ciclo_aberto = [r for r in resultados if r["resultado"] == "ciclo_aberto"]
    aptos = [r for r in resultados if r["ficara_apto_renovar"]]
    inativos_com_desconto = [r for r in resultados if r["desconto_em_associado_inativo"]]
    associados_importados = [r for r in resultados if r.get("associado_importado")]

    valor_previsto = sum((_to_decimal(r["valor_descontado"]) for r in resultados), Decimal("0"))
    valor_real = sum((_to_decimal(r["valor_descontado"]) for r in baixa), Decimal("0"))

    v3050 = [r for r in resultados if r["categoria"] == "valores_30_50"]
    v3050_desc = [r for r in v3050 if r["resultado"] == "baixa_efetuada"]
    v3050_ndesc = [r for r in v3050 if r["resultado"] == "nao_descontado"]

    # Mudanças de status de associado
    mudancas_assoc: dict[tuple, int] = defaultdict(int)
    for r in resultados:
        antes = r.get("associado_status_antes")
        depois = r.get("associado_status_depois")
        if antes and depois and antes != depois:
            mudancas_assoc[(antes, depois)] += 1

    # Mudanças de status de ciclo
    mudancas_ciclo: dict[tuple, int] = defaultdict(int)
    for r in resultados:
        antes = r.get("ciclo_status_antes")
        depois = r.get("ciclo_status_depois")
        if antes and depois and antes != depois:
            mudancas_ciclo[(antes, depois)] += 1

    return {
        "total_no_arquivo": total,
        "atualizados": len(baixa) + len(nao_desc) + len(pendencia) + len(nao_enc),
        "baixa_efetuada": len(baixa),
        "nao_descontado": len(nao_desc),
        "nao_encontrado": len(nao_enc),
        "associados_importados": len(associados_importados),
        "pendencia_manual": len(pendencia),
        "ciclo_aberto": len(ciclo_aberto),
        "valor_previsto": str(valor_previsto),
        "valor_real": str(valor_real),
        "aptos_a_renovar": len(aptos),
        "associados_inativos_com_desconto": len(inativos_com_desconto),
        "valores_30_50": {
            "descontaram": {
                "count": len(v3050_desc),
                "valor_total": str(sum((_to_decimal(r["valor_descontado"]) for r in v3050_desc), Decimal("0"))),
            },
            "nao_descontaram": {
                "count": len(v3050_ndesc),
                "valor_total": str(sum((_to_decimal(r["valor_descontado"]) for r in v3050_ndesc), Decimal("0"))),
            },
        },
        "mudancas_status_associado": [
            {"antes": antes, "depois": depois, "count": count}
            for (antes, depois), count in sorted(mudancas_assoc.items())
        ],
        "mudancas_status_ciclo": [
            {"antes": antes, "depois": depois, "count": count}
            for (antes, depois), count in sorted(mudancas_ciclo.items())
        ],
    }


def simular_dry_run(competencia: date, parsed_items: list[dict]) -> dict:
    """Ponto de entrada principal.

    Recebe os items já parseados (sem deduplicação — chame _deduplicar antes),
    simula a reconciliação de forma read-only e retorna o dict com kpis + items.
    """
    resultados = [_simular_item(raw, competencia) for raw in parsed_items]
    _apply_renewal_transition_flags(resultados, competencia)
    serialized_results = _strip_internal_fields(resultados)
    return {
        "kpis": _build_kpis(serialized_results),
        "items": serialized_results,
    }
