from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.exceptions import ValidationError

from apps.contratos.canonicalization import (
    get_operational_contracts_for_associado,
    resolve_operational_contract_for_associado,
)
from apps.contratos.cycle_projection import (
    ACTIVE_OPERATIONAL_REFINANCIAMENTO_STATUSES,
    APT_LIKE_OPERATIONAL_REFINANCIAMENTO_STATUSES,
    STATUS_VISUAL_FINANCIAL_LABELS,
    STATUS_VISUAL_PHASE_LABELS,
    build_contract_cycle_projection,
    is_contract_eligible_for_renewal_competencia,
    resolve_associado_mother_status,
    resolve_current_renewal_competencia,
    sync_associado_mother_status,
)
from apps.contratos.cycle_rebuild import rebind_financial_links_by_reference
from apps.contratos.cycle_rebuild import rebuild_contract_cycle_state
from apps.contratos.cycle_timeline import (
    get_cycle_activation_payload,
    get_future_generation_threshold,
    get_next_cycle,
)
from apps.contratos.models import Ciclo, Contrato, Parcela
from apps.contratos.small_value_rules import is_dedicated_small_value_contract
from apps.esteira.models import EsteiraItem, Transicao
from apps.importacao.models import ArquivoRetornoItem
from apps.refinanciamento.models import Assumption, Comprovante, Item, Refinanciamento
from apps.tesouraria.models import BaixaManual, LiquidacaoContratoItem
from core.file_references import build_filefield_reference

from .models import (
    AdminOverrideChange,
    AdminOverrideEvent,
    Associado,
    Documento,
)


class AdminOverrideConflict(Exception):
    """Raised when the admin is editing stale data."""


@dataclass
class _OverrideContext:
    associado: Associado
    contrato: Contrato | None = None
    ciclo: Ciclo | None = None
    parcela: Parcela | None = None
    refinanciamento: Refinanciamento | None = None
    documento: Documento | None = None
    comprovante: Comprovante | None = None


RESOLVED_UNPAID_STATUSES = {
    Parcela.Status.DESCONTADO,
    Parcela.Status.LIQUIDADA,
    "quitada",
}
CONCLUDED_CYCLE_STATUSES = {
    Ciclo.Status.CICLO_RENOVADO,
    Ciclo.Status.FECHADO,
}
SAFE_RENEWAL_STAGE_LABELS = {
    Refinanciamento.Status.APTO_A_RENOVAR: "Apto a renovar",
    Refinanciamento.Status.EM_ANALISE_RENOVACAO: "Em análise",
    Refinanciamento.Status.PENDENTE_TERMO_AGENTE: "Pendente termo do agente",
    Refinanciamento.Status.PENDENTE_TERMO_ANALISTA: "Pendente termo do analista",
    Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO: "Aprovado pela análise",
    Refinanciamento.Status.APROVADO_PARA_RENOVACAO: "Aprovado para renovação",
    Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO: "Solicitado para liquidação",
    Refinanciamento.Status.EFETIVADO: "Efetivar renovação",
    Refinanciamento.Status.REVERTIDO: "Cancelar renovação e manter ativo",
}
LEGACY_INACTIVATION_SOURCE_STATUSES = {
    Associado.Status.INATIVO,
    Associado.Status.INADIMPLENTE,
    Associado.Status.APTO_A_RENOVAR,
}
LEGACY_INACTIVATION_RETURN_STATUSES = {
    Associado.Status.CADASTRADO,
    Associado.Status.IMPORTADO,
    Associado.Status.EM_ANALISE,
    Associado.Status.ATIVO,
    Associado.Status.PENDENTE,
    Associado.Status.APTO_A_RENOVAR,
}
LEGACY_INACTIVATION_RETURN_QUEUE_STATUSES = {
    EsteiraItem.Situacao.AGUARDANDO,
    EsteiraItem.Situacao.EM_ANDAMENTO,
    EsteiraItem.Situacao.PENDENCIADO,
    EsteiraItem.Situacao.APROVADO,
}
ADMIN_REFINANCIAMENTO_EDITOR_STATUS_ERROR = (
    'O status da renovação não pode ser salvo pelo editor avançado. '
    'Use "Enviar para etapa" para reposicionar a fila e a tesouraria para efetivar.'
)


def _parse_decimal(value: Any, default: Decimal = Decimal("0.00")) -> Decimal:
    if value in (None, ""):
        return default
    return Decimal(str(value))


def _assert_override_mensalidade_allowed(
    *,
    current_value: Any,
    next_value: Any,
    field_name: str,
) -> Decimal:
    mensalidade = _parse_decimal(next_value)
    if mensalidade > 0:
        return mensalidade

    current_mensalidade = _parse_decimal(current_value)
    if current_mensalidade <= 0 and mensalidade <= 0:
        return mensalidade

    raise ValidationError(
        {
            field_name: [
                "A mensalidade deve ser maior que zero para novos fluxos operacionais."
            ]
        }
    )


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _parse_optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    parsed = parse_date(str(value))
    if parsed is None:
        raise ValidationError("Data inválida.")
    return parsed


def _normalize_cycle_status(value: Any, default: str = Ciclo.Status.ABERTO) -> str:
    if value in (None, ""):
        return default
    normalized = str(value).strip().lower()
    if not normalized:
        return default
    if normalized == "concluido":
        return Ciclo.Status.FECHADO
    return normalized


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    return value


def _is_unresolved_unpaid_row(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "") not in RESOLVED_UNPAID_STATUSES


def _normalize_dt(value):
    if value is None:
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value.astimezone(timezone.get_current_timezone())


def _assert_version(obj, provided_updated_at: str | None) -> None:
    if not provided_updated_at:
        return
    current = _normalize_dt(getattr(obj, "updated_at", None))
    provided = _normalize_dt(parse_datetime(str(provided_updated_at)))
    if current is None or provided is None:
        return
    if current.replace(microsecond=0) != provided.replace(microsecond=0):
        raise AdminOverrideConflict(
            "Os dados foram alterados por outro usuário. Recarregue a página antes de salvar."
        )


def _warning_payload(
    *,
    code: str,
    contrato: Contrato,
    message: str,
    details: dict[str, Any],
    scope: str | None = None,
    competencia: date | str | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    resolved_competencia: str | None
    if isinstance(competencia, date):
        resolved_competencia = competencia.isoformat()
    elif competencia in (None, ""):
        resolved_competencia = None
    else:
        resolved_competencia = str(competencia)
    return {
        "code": code,
        "severity": "warning",
        "contrato_id": contrato.id,
        "contrato_codigo": contrato.codigo or "",
        "scope": scope or "",
        "competencia": resolved_competencia,
        "action": action or "",
        "message": message,
        "details": details,
    }


def _reference_month_start(value: date | None) -> date | None:
    if value is None:
        return None
    return value.replace(day=1)


def _build_contract_layout_warnings(
    contrato: Contrato,
    *,
    cycles_payload: list[dict[str, Any]] | None = None,
    parcel_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []

    normalized_cycles: list[dict[str, Any]] = []
    cycle_ref_map: dict[str, dict[str, Any]] = {}
    if cycles_payload is None:
        cycles = list(
            contrato.ciclos.filter(deleted_at__isnull=True).order_by("numero", "id")
        )
        for cycle in cycles:
            normalized = {
                "id": cycle.id,
                "ref": str(cycle.id),
                "numero": cycle.numero,
                "data_inicio": cycle.data_inicio,
                "data_fim": cycle.data_fim,
                "status": cycle.status,
            }
            normalized_cycles.append(normalized)
            cycle_ref_map[str(cycle.id)] = normalized
    else:
        for raw_cycle in cycles_payload:
            data_inicio = _parse_optional_date(raw_cycle.get("data_inicio"))
            data_fim = _parse_optional_date(raw_cycle.get("data_fim"))
            normalized = {
                "id": raw_cycle.get("id"),
                "ref": str(raw_cycle.get("client_key") or raw_cycle.get("id") or ""),
                "numero": int(raw_cycle.get("numero") or 0),
                "data_inicio": data_inicio,
                "data_fim": data_fim,
                "status": _normalize_cycle_status(raw_cycle.get("status"), default=""),
            }
            normalized_cycles.append(normalized)
            if raw_cycle.get("id") is not None:
                cycle_ref_map[str(raw_cycle["id"])] = normalized
            if raw_cycle.get("client_key"):
                cycle_ref_map[str(raw_cycle["client_key"])] = normalized

    sorted_cycles = sorted(
        [cycle for cycle in normalized_cycles if cycle["data_inicio"] and cycle["data_fim"]],
        key=lambda cycle: (cycle["data_inicio"], cycle["numero"]),
    )
    for index, left in enumerate(sorted_cycles):
        for right in sorted_cycles[index + 1 :]:
            if right["data_inicio"] > left["data_fim"]:
                break
            if left["data_inicio"] <= right["data_fim"] and right["data_inicio"] <= left["data_fim"]:
                warnings.append(
                    _warning_payload(
                        code="cycle_date_overlap",
                        contrato=contrato,
                        scope="cycle_layout",
                        action="review_cycle_dates",
                        message=(
                            f"Ciclos {left['numero']} e {right['numero']} possuem datas sobrepostas."
                        ),
                        details={
                            "cycle_numbers": [left["numero"], right["numero"]],
                            "left_range": [
                                left["data_inicio"].isoformat(),
                                left["data_fim"].isoformat(),
                            ],
                            "right_range": [
                                right["data_inicio"].isoformat(),
                                right["data_fim"].isoformat(),
                            ],
                        },
                    )
                )

    normalized_parcelas: list[dict[str, Any]] = []
    if parcel_items is None:
        parcelas = (
            Parcela.all_objects.filter(ciclo__contrato=contrato, deleted_at__isnull=True)
            .exclude(status=Parcela.Status.CANCELADO)
            .select_related("ciclo")
            .order_by("ciclo__numero", "numero", "id")
        )
        for parcela in parcelas:
            normalized_parcelas.append(
                {
                    "id": parcela.id,
                    "numero": parcela.numero,
                    "referencia_mes": parcela.referencia_mes,
                    "cycle_number": parcela.ciclo.numero,
                    "cycle_start": parcela.ciclo.data_inicio,
                    "cycle_end": parcela.ciclo.data_fim,
                }
            )
    else:
        for raw_parcela in parcel_items:
            cycle_ref = raw_parcela.get("cycle_ref") or raw_parcela.get("cycle_id")
            cycle = cycle_ref_map.get(str(cycle_ref))
            normalized_parcelas.append(
                {
                    "id": raw_parcela.get("id"),
                    "numero": int(raw_parcela.get("numero") or 0),
                    "referencia_mes": _parse_optional_date(raw_parcela.get("referencia_mes")),
                    "cycle_number": cycle["numero"] if cycle else None,
                    "cycle_start": cycle["data_inicio"] if cycle else None,
                    "cycle_end": cycle["data_fim"] if cycle else None,
                }
            )

    parcelas_by_reference: defaultdict[date, list[dict[str, Any]]] = defaultdict(list)
    for parcela in normalized_parcelas:
        referencia_mes = parcela["referencia_mes"]
        if referencia_mes is None:
            continue
        parcelas_by_reference[referencia_mes].append(parcela)

        cycle_start = _reference_month_start(parcela.get("cycle_start"))
        cycle_end = _reference_month_start(parcela.get("cycle_end"))
        referencia_inicio = _reference_month_start(referencia_mes)
        if (
            cycle_start is not None
            and cycle_end is not None
            and referencia_inicio is not None
            and (referencia_inicio < cycle_start or referencia_inicio > cycle_end)
        ):
            warnings.append(
                _warning_payload(
                    code="parcela_outside_cycle_range",
                    contrato=contrato,
                    scope="cycle_layout",
                    competencia=referencia_mes,
                    action="move_reference_into_cycle_range",
                    message=(
                        f"A competência {referencia_mes.strftime('%m/%Y')} está fora do intervalo do ciclo "
                        f"{parcela.get('cycle_number') or '-'}."
                    ),
                    details={
                        "referencia_mes": referencia_mes.isoformat(),
                        "cycle_number": parcela.get("cycle_number"),
                        "cycle_range": [
                            cycle_start.isoformat(),
                            cycle_end.isoformat(),
                        ],
                    },
                )
            )

    for referencia_mes, parcelas in sorted(parcelas_by_reference.items()):
        if len(parcelas) < 2:
            continue
        warnings.append(
            _warning_payload(
                code="duplicate_reference_month",
                contrato=contrato,
                scope="cycle_layout",
                competencia=referencia_mes,
                action="review_duplicate_reference",
                message=(
                    f"A competência {referencia_mes.strftime('%m/%Y')} aparece em mais de uma parcela."
                ),
                details={
                    "referencia_mes": referencia_mes.isoformat(),
                    "parcelas": [
                        {
                            "id": parcela.get("id"),
                            "numero": parcela.get("numero"),
                            "cycle_number": parcela.get("cycle_number"),
                        }
                        for parcela in parcelas
                    ],
                },
            )
        )

    unique_warnings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for warning in warnings:
        key = (str(warning["code"]), str(warning["message"]))
        if key in seen:
            continue
        seen.add(key)
        unique_warnings.append(warning)
    return unique_warnings


def _renewal_stage_label(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return "Sem fila operacional materializada"
    return SAFE_RENEWAL_STAGE_LABELS.get(
        normalized,
        normalized.replace("_", " ").capitalize(),
    )


def _latest_projection_cycle(projection: dict[str, Any]) -> dict[str, Any] | None:
    cycles = list(sorted(projection.get("cycles") or [], key=lambda item: item["numero"]))
    return cycles[-1] if cycles else None


def _is_paid_projection_status(value: Any) -> bool:
    return str(value or "") in {
        Parcela.Status.DESCONTADO,
        Parcela.Status.LIQUIDADA,
        "quitada",
    }


def _build_renewal_queue_warnings(
    contrato: Contrato,
    *,
    projection: dict[str, Any],
    refinanciamento_ativo: Refinanciamento | None,
) -> list[dict[str, Any]]:
    if (
        contrato.origem_operacional == Contrato.OrigemOperacional.REATIVACAO
        and contrato.auxilio_liberado_em is None
        and contrato.status
        not in {
            Contrato.Status.ATIVO,
            Contrato.Status.CANCELADO,
            Contrato.Status.ENCERRADO,
        }
    ):
        return []

    latest_cycle = _latest_projection_cycle(projection)
    latest_cycle_status = str((latest_cycle or {}).get("status") or "")
    latest_cycle_parcelas = list((latest_cycle or {}).get("parcelas") or [])
    projection_status = ""
    if (
        latest_cycle_status == Ciclo.Status.APTO_A_RENOVAR
        and is_contract_eligible_for_renewal_competencia(
            contrato,
            projection=projection,
            parcelas=latest_cycle_parcelas,
        )
    ):
        projection_status = Refinanciamento.Status.APTO_A_RENOVAR
    current_competencia = resolve_current_renewal_competencia()

    if projection_status == Refinanciamento.Status.APTO_A_RENOVAR and refinanciamento_ativo is None:
        return [
            _warning_payload(
                code="renewal_queue_missing",
                contrato=contrato,
                scope="renewal_queue",
                competencia=current_competencia,
                action="use_send_to_stage",
                message=(
                    "O contrato aparece como apto na projeção, mas não possui linha operacional "
                    "materializada na fila de renovação."
                ),
                details={
                    "projection_status": projection_status,
                    "projection_status_label": _renewal_stage_label(projection_status),
                    "operational_status": "",
                    "operational_status_label": _renewal_stage_label(""),
                    "competencia_atual": current_competencia.isoformat(),
                },
            )
        ]

    if refinanciamento_ativo is None:
        return []

    operational_status = str(refinanciamento_ativo.status or "")
    if (
        projection_status == Refinanciamento.Status.APTO_A_RENOVAR
        or operational_status in APT_LIKE_OPERATIONAL_REFINANCIAMENTO_STATUSES
    ):
        return []

    return [
        _warning_payload(
            code="renewal_queue_divergence",
            contrato=contrato,
            scope="renewal_queue",
            competencia=current_competencia,
            action="use_send_to_stage",
            message=(
                "A etapa operacional da renovação permanece ativa, mas a competência atual do contrato "
                "não sustenta essa posição automaticamente. O admin pode corrigir isso usando "
                "'Enviar para etapa'."
            ),
            details={
                "projection_status": projection_status,
                "projection_status_label": _renewal_stage_label(projection_status),
                "operational_status": operational_status,
                "operational_status_label": _renewal_stage_label(operational_status),
                "competencia_atual": current_competencia.isoformat(),
                "refinanciamento_id": refinanciamento_ativo.id,
            },
        )
    ]


def _file_reference_payload(filefield, *, request=None) -> dict[str, Any]:
    stored_path = str(getattr(filefield, "name", "") or "")
    reference = build_filefield_reference(
        filefield,
        request=request,
        missing_type="legado_sem_arquivo",
    )
    return {
        "url": reference.url,
        "arquivo_referencia": reference.arquivo_referencia or stored_path,
        "arquivo_disponivel_localmente": reference.arquivo_disponivel_localmente,
        "tipo_referencia": reference.tipo_referencia,
    }


def _serialize_documento(documento: Documento, *, request=None) -> dict[str, Any]:
    file_payload = _file_reference_payload(documento.arquivo, request=request)
    return {
        "id": documento.id,
        "tipo": documento.tipo,
        "status": documento.status,
        "observacao": documento.observacao,
        "origem": documento.origem,
        "nome_original": documento.nome_original,
        "created_at": documento.created_at.isoformat() if documento.created_at else None,
        "updated_at": documento.updated_at.isoformat() if documento.updated_at else None,
        "deleted_at": documento.deleted_at.isoformat() if documento.deleted_at else None,
        **file_payload,
    }


def _serialize_comprovante(comprovante: Comprovante, *, request=None) -> dict[str, Any]:
    file_payload = _file_reference_payload(comprovante.arquivo, request=request)
    stored_path = str(getattr(comprovante.arquivo, "name", "") or "")
    arquivo = (
        file_payload.get("url")
        or stored_path
        or file_payload.get("arquivo_referencia")
        or ""
    )
    return {
        "id": comprovante.id,
        "tipo": comprovante.tipo,
        "papel": comprovante.papel,
        "arquivo": arquivo,
        "origem": comprovante.origem,
        "status_validacao": comprovante.status_validacao,
        "nome_original": comprovante.nome_original,
        "mime": comprovante.mime,
        "size_bytes": comprovante.size_bytes,
        "data_pagamento": (
            comprovante.data_pagamento.isoformat()
            if comprovante.data_pagamento
            else None
        ),
        "created_at": comprovante.created_at.isoformat() if comprovante.created_at else None,
        "updated_at": comprovante.updated_at.isoformat() if comprovante.updated_at else None,
        "deleted_at": comprovante.deleted_at.isoformat() if comprovante.deleted_at else None,
        "legacy_comprovante_id": comprovante.legacy_comprovante_id,
        "contrato_id": comprovante.contrato_id,
        "ciclo_id": comprovante.ciclo_id,
        "refinanciamento_id": comprovante.refinanciamento_id,
        **file_payload,
    }


def _serialize_parcela(parcela: Parcela) -> dict[str, Any]:
    return {
        "id": parcela.id,
        "ciclo_id": parcela.ciclo_id,
        "numero": parcela.numero,
        "referencia_mes": parcela.referencia_mes.isoformat(),
        "valor": str(parcela.valor),
        "data_vencimento": parcela.data_vencimento.isoformat(),
        "status": parcela.status,
        "layout_bucket": parcela.layout_bucket,
        "data_pagamento": (
            parcela.data_pagamento.isoformat() if parcela.data_pagamento else None
        ),
        "observacao": parcela.observacao,
        "updated_at": parcela.updated_at.isoformat() if parcela.updated_at else None,
        "deleted_at": parcela.deleted_at.isoformat() if parcela.deleted_at else None,
    }


def _serialize_ciclo(ciclo: Ciclo, *, include_parcelas: bool = True) -> dict[str, Any]:
    payload = {
        "id": ciclo.id,
        "numero": ciclo.numero,
        "data_inicio": ciclo.data_inicio.isoformat(),
        "data_fim": ciclo.data_fim.isoformat(),
        "status": ciclo.status,
        "valor_total": str(ciclo.valor_total),
        "updated_at": ciclo.updated_at.isoformat() if ciclo.updated_at else None,
        "deleted_at": ciclo.deleted_at.isoformat() if ciclo.deleted_at else None,
    }
    if include_parcelas:
        payload["parcelas"] = [
            _serialize_parcela(parcela)
            for parcela in Parcela.all_objects.filter(
                ciclo=ciclo,
                deleted_at__isnull=True,
            )
            .exclude(status=Parcela.Status.CANCELADO)
            .order_by("numero", "id")
        ]
    return payload


def _serialize_contrato(contrato: Contrato, *, include_ciclos: bool = True) -> dict[str, Any]:
    payload = {
        "id": contrato.id,
        "codigo": contrato.codigo,
        "status": contrato.status,
        "valor_bruto": str(contrato.valor_bruto),
        "valor_liquido": str(contrato.valor_liquido),
        "valor_mensalidade": str(contrato.valor_mensalidade),
        "prazo_meses": contrato.prazo_meses,
        "taxa_antecipacao": str(contrato.taxa_antecipacao),
        "margem_disponivel": str(contrato.margem_disponivel),
        "valor_total_antecipacao": str(contrato.valor_total_antecipacao),
        "doacao_associado": str(contrato.doacao_associado),
        "comissao_agente": str(contrato.comissao_agente),
        "data_contrato": contrato.data_contrato.isoformat() if contrato.data_contrato else None,
        "data_aprovacao": contrato.data_aprovacao.isoformat() if contrato.data_aprovacao else None,
        "data_primeira_mensalidade": (
            contrato.data_primeira_mensalidade.isoformat()
            if contrato.data_primeira_mensalidade
            else None
        ),
        "mes_averbacao": contrato.mes_averbacao.isoformat() if contrato.mes_averbacao else None,
        "auxilio_liberado_em": (
            contrato.auxilio_liberado_em.isoformat() if contrato.auxilio_liberado_em else None
        ),
        "agente_id": contrato.agente_id,
        "admin_manual_layout_enabled": contrato.admin_manual_layout_enabled,
        "updated_at": contrato.updated_at.isoformat() if contrato.updated_at else None,
    }
    if include_ciclos:
        payload["ciclos"] = [
            _serialize_ciclo(ciclo)
            for ciclo in contrato.ciclos.filter(deleted_at__isnull=True).order_by("numero", "id")
        ]
    return payload


def _serialize_associado(associado: Associado) -> dict[str, Any]:
    return {
        "id": associado.id,
        "matricula": associado.matricula,
        "tipo_documento": associado.tipo_documento,
        "nome_completo": associado.nome_completo,
        "cpf_cnpj": associado.cpf_cnpj,
        "rg": associado.rg,
        "orgao_expedidor": associado.orgao_expedidor,
        "email": associado.email,
        "telefone": associado.telefone,
        "data_nascimento": (
            associado.data_nascimento.isoformat() if associado.data_nascimento else None
        ),
        "profissao": associado.profissao,
        "estado_civil": associado.estado_civil,
        "orgao_publico": associado.orgao_publico,
        "matricula_orgao": associado.matricula_orgao,
        "cargo": associado.cargo,
        "status": resolve_associado_mother_status(associado),
        "observacao": associado.observacao,
        "agente_responsavel_id": associado.agente_responsavel_id,
        "percentual_repasse": str(associado.auxilio_taxa),
        "endereco": associado.build_endereco_payload(),
        "dados_bancarios": associado.build_dados_bancarios_payload(),
        "contato": associado.build_contato_payload(),
        "updated_at": associado.updated_at.isoformat() if associado.updated_at else None,
    }


def _serialize_refinanciamento(refinanciamento: Refinanciamento) -> dict[str, Any]:
    return {
        "id": refinanciamento.id,
        "status": refinanciamento.status,
        "competencia_solicitada": refinanciamento.competencia_solicitada.isoformat(),
        "valor_refinanciamento": str(refinanciamento.valor_refinanciamento),
        "repasse_agente": str(refinanciamento.repasse_agente),
        "executado_em": refinanciamento.executado_em.isoformat() if refinanciamento.executado_em else None,
        "data_ativacao_ciclo": (
            refinanciamento.data_ativacao_ciclo.isoformat()
            if refinanciamento.data_ativacao_ciclo
            else None
        ),
        "motivo_bloqueio": refinanciamento.motivo_bloqueio,
        "observacao": refinanciamento.observacao,
        "analista_note": refinanciamento.analista_note,
        "coordenador_note": refinanciamento.coordenador_note,
        "reviewed_by_id": refinanciamento.reviewed_by_id,
        "updated_at": refinanciamento.updated_at.isoformat() if refinanciamento.updated_at else None,
    }


def _serialize_refinanciamento_for_editor(
    refinanciamento: Refinanciamento,
    contrato: Contrato,
) -> dict[str, Any]:
    payload = _serialize_refinanciamento(refinanciamento)
    margem_disponivel = Decimal(str(contrato.margem_disponivel or "0.00"))
    if margem_disponivel > 0:
        payload["valor_refinanciamento"] = str(margem_disponivel)
    return payload


def _serialize_esteira(esteira: EsteiraItem | None) -> dict[str, Any]:
    if esteira is None:
        return {}
    return {
        "id": esteira.id,
        "etapa_atual": esteira.etapa_atual,
        "status": esteira.status,
        "prioridade": esteira.prioridade,
        "observacao": esteira.observacao,
        "assumido_em": esteira.assumido_em.isoformat() if esteira.assumido_em else None,
        "heartbeat_at": esteira.heartbeat_at.isoformat() if esteira.heartbeat_at else None,
        "analista_responsavel_id": esteira.analista_responsavel_id,
        "coordenador_responsavel_id": esteira.coordenador_responsavel_id,
        "tesoureiro_responsavel_id": esteira.tesoureiro_responsavel_id,
        "concluido_em": esteira.concluido_em.isoformat() if esteira.concluido_em else None,
        "updated_at": esteira.updated_at.isoformat() if esteira.updated_at else None,
    }


def _dedupe_warning_list(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique_warnings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for warning in warnings:
        key = (
            str(warning.get("code") or ""),
            str(warning.get("scope") or ""),
            str(warning.get("competencia") or ""),
            str(warning.get("message") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique_warnings.append(warning)
    return unique_warnings


def _sanitize_save_all_refinanciamento_payload(
    *,
    refinanciamento: Refinanciamento,
    contrato: Contrato,
    payload: dict[str, Any],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    sanitized = dict(payload)
    requested_status = str(sanitized.pop("status", "") or "").strip()
    if requested_status and requested_status != refinanciamento.status:
        warnings.append(
            _warning_payload(
                code="renewal_status_ignored_in_save_all",
                contrato=contrato,
                scope="refinanciamento",
                competencia=refinanciamento.competencia_solicitada,
                action="use_safe_transition",
                message=(
                    "O status da renovação foi ignorado no salvar em lote. "
                    'Use "Enviar para etapa" para reposicionar a fila operacional.'
                ),
                details={
                    "refinanciamento_id": refinanciamento.id,
                    "status_atual": refinanciamento.status,
                    "status_solicitado": requested_status,
                },
            )
        )
    return sanitized


def _contract_core_payload_has_effective_changes(
    contrato: Contrato,
    payload: dict[str, Any],
) -> bool:
    current = _serialize_contrato(contrato, include_ciclos=False)
    comparable_fields = [
        "status",
        "valor_bruto",
        "valor_liquido",
        "valor_mensalidade",
        "prazo_meses",
        "taxa_antecipacao",
        "margem_disponivel",
        "valor_total_antecipacao",
        "doacao_associado",
        "comissao_agente",
        "data_contrato",
        "data_aprovacao",
        "data_primeira_mensalidade",
        "mes_averbacao",
        "auxilio_liberado_em",
    ]
    for field in comparable_fields:
        if field not in payload:
            continue
        current_value = current.get(field)
        next_value = payload.get(field)
        if field.startswith("data_") or field in {"mes_averbacao", "auxilio_liberado_em"}:
            normalized_next = _parse_optional_date(next_value)
            if (current_value or None) != (
                normalized_next.isoformat() if normalized_next else None
            ):
                return True
            continue
        if field == "prazo_meses":
            try:
                if int(current_value or 0) != int(next_value or 0):
                    return True
            except (TypeError, ValueError):
                return True
            continue
        if field in {
            "valor_bruto",
            "valor_liquido",
            "valor_mensalidade",
            "taxa_antecipacao",
            "margem_disponivel",
            "valor_total_antecipacao",
            "doacao_associado",
            "comissao_agente",
        }:
            if str(current_value or "0.00") != str(_parse_decimal(next_value)):
                return True
            continue
        if str(current_value or "") != str(next_value or ""):
            return True
    return False


def _refinanciamento_payload_has_effective_changes(
    refinanciamento: Refinanciamento,
    payload: dict[str, Any],
) -> bool:
    current = _serialize_refinanciamento(refinanciamento)
    comparable_fields = [
        "competencia_solicitada",
        "valor_refinanciamento",
        "repasse_agente",
        "executado_em",
        "data_ativacao_ciclo",
        "motivo_bloqueio",
        "observacao",
        "analista_note",
        "coordenador_note",
        "reviewed_by_id",
    ]
    for field in comparable_fields:
        if field not in payload:
            continue
        current_value = current.get(field)
        next_value = payload.get(field)
        if field == "competencia_solicitada":
            normalized_next = _parse_optional_date(next_value)
            if (current_value or None) != (
                normalized_next.isoformat() if normalized_next else None
            ):
                return True
            continue
        if field in {"executado_em", "data_ativacao_ciclo"}:
            normalized_next = parse_datetime(str(next_value)) if next_value else None
            if (current_value or None) != (
                normalized_next.isoformat() if normalized_next else None
            ):
                return True
            continue
        if field in {"valor_refinanciamento", "repasse_agente"}:
            if str(current_value or "0.00") != str(_parse_decimal(next_value)):
                return True
            continue
        if str(current_value or "") != str(next_value or ""):
            return True
    return False


def _normalize_cycle_layout_payload_for_compare(payload: dict[str, Any]) -> dict[str, Any]:
    normalized_cycles = sorted(
        [
            {
                "id": raw_cycle.get("id"),
                "numero": int(raw_cycle.get("numero") or 0),
                "data_inicio": (
                    _parse_optional_date(raw_cycle.get("data_inicio")).isoformat()
                    if _parse_optional_date(raw_cycle.get("data_inicio"))
                    else None
                ),
                "data_fim": (
                    _parse_optional_date(raw_cycle.get("data_fim")).isoformat()
                    if _parse_optional_date(raw_cycle.get("data_fim"))
                    else None
                ),
                "status": _normalize_cycle_status(raw_cycle.get("status")),
                "valor_total": str(_parse_decimal(raw_cycle.get("valor_total"))),
            }
            for raw_cycle in (payload.get("cycles") or [])
        ],
        key=lambda item: (int(item["numero"]), int(item["id"] or 0)),
    )
    normalized_parcelas = sorted(
        [
            {
                "id": raw_parcela.get("id"),
                "cycle_ref": str(
                    raw_parcela.get("cycle_ref")
                    or raw_parcela.get("cycle_id")
                    or ""
                ),
                "numero": int(raw_parcela.get("numero") or 0),
                "referencia_mes": (
                    _parse_optional_date(raw_parcela.get("referencia_mes")).isoformat()
                    if _parse_optional_date(raw_parcela.get("referencia_mes"))
                    else None
                ),
                "valor": str(_parse_decimal(raw_parcela.get("valor"))),
                "data_vencimento": (
                    _parse_optional_date(raw_parcela.get("data_vencimento")).isoformat()
                    if _parse_optional_date(raw_parcela.get("data_vencimento"))
                    else None
                ),
                "status": str(raw_parcela.get("status") or Parcela.Status.EM_PREVISAO),
                "data_pagamento": (
                    _parse_optional_date(raw_parcela.get("data_pagamento")).isoformat()
                    if _parse_optional_date(raw_parcela.get("data_pagamento"))
                    else None
                ),
                "observacao": str(raw_parcela.get("observacao") or ""),
                "layout_bucket": str(
                    raw_parcela.get("layout_bucket") or Parcela.LayoutBucket.CYCLE
                ),
            }
            for raw_parcela in (payload.get("parcelas") or [])
            if str(raw_parcela.get("status") or "") != Parcela.Status.CANCELADO
        ],
        key=lambda item: (
            str(item["cycle_ref"]),
            int(item["numero"]),
            str(item["referencia_mes"] or ""),
            int(item["id"] or 0),
        ),
    )
    return {
        "cycles": normalized_cycles,
        "parcelas": normalized_parcelas,
    }


def _current_cycle_layout_for_compare(contrato: Contrato) -> dict[str, Any]:
    current_cycles = [
        {
            "id": ciclo.id,
            "numero": ciclo.numero,
            "data_inicio": ciclo.data_inicio.isoformat() if ciclo.data_inicio else None,
            "data_fim": ciclo.data_fim.isoformat() if ciclo.data_fim else None,
            "status": _normalize_cycle_status(ciclo.status),
            "valor_total": str(ciclo.valor_total),
        }
        for ciclo in contrato.ciclos.filter(deleted_at__isnull=True).order_by("numero", "id")
    ]
    current_parcelas = [
        {
            "id": parcela.id,
            "cycle_ref": str(parcela.ciclo_id),
            "numero": parcela.numero,
            "referencia_mes": parcela.referencia_mes.isoformat()
            if parcela.referencia_mes
            else None,
            "valor": str(parcela.valor),
            "data_vencimento": parcela.data_vencimento.isoformat()
            if parcela.data_vencimento
            else None,
            "status": parcela.status,
            "data_pagamento": parcela.data_pagamento.isoformat()
            if parcela.data_pagamento
            else None,
            "observacao": parcela.observacao or "",
            "layout_bucket": parcela.layout_bucket,
        }
        for parcela in (
            Parcela.all_objects.filter(ciclo__contrato=contrato, deleted_at__isnull=True)
            .exclude(status=Parcela.Status.CANCELADO)
            .select_related("ciclo")
            .order_by("ciclo__numero", "numero", "id")
        )
    ]
    return {
        "cycles": current_cycles,
        "parcelas": current_parcelas,
    }


def _cycle_layout_payload_has_effective_changes(
    contrato: Contrato,
    payload: dict[str, Any],
) -> bool:
    if not contrato.admin_manual_layout_enabled:
        return True
    return _normalize_cycle_layout_payload_for_compare(payload) != _current_cycle_layout_for_compare(
        contrato
    )


def _esteira_payload_has_effective_changes(
    associado: Associado,
    payload: dict[str, Any],
) -> bool:
    current = _serialize_esteira(getattr(associado, "esteira_item", None))
    if not current:
        return bool(payload)
    comparable_fields = ["etapa_atual", "status", "prioridade", "observacao"]
    for field in comparable_fields:
        if field not in payload:
            continue
        if str(current.get(field) or "") != str(payload.get(field) or ""):
            return True
    return False


def _normalize_esteira_conclusion_state(
    esteira: EsteiraItem | None,
    *,
    explicit_transition: bool = False,
) -> None:
    if esteira is None:
        return
    changed_fields: list[str] = []
    if esteira.etapa_atual == EsteiraItem.Etapa.CONCLUIDO:
        if explicit_transition and esteira.concluido_em is None:
            esteira.concluido_em = timezone.now()
            changed_fields.append("concluido_em")
    elif esteira.concluido_em is not None:
        esteira.concluido_em = None
        changed_fields.append("concluido_em")
    if changed_fields:
        esteira.save(update_fields=[*changed_fields, "updated_at"])


def _parcel_financial_flags(parcela: Parcela | None) -> dict[str, bool]:
    if parcela is None:
        return {
            "tem_retorno": False,
            "tem_baixa_manual": False,
            "tem_liquidacao": False,
        }
    return {
        "tem_retorno": ArquivoRetornoItem.objects.filter(parcela=parcela).exists(),
        "tem_baixa_manual": BaixaManual.objects.filter(parcela=parcela).exists(),
        "tem_liquidacao": LiquidacaoContratoItem.objects.filter(parcela=parcela).exists(),
    }


def _build_editor_item(
    item: dict[str, Any],
    *,
    actual_parcela: Parcela | None,
    bucket: str,
) -> dict[str, Any]:
    resolved_bucket = (
        bucket
        if actual_parcela is None or actual_parcela.layout_bucket != bucket
        else actual_parcela.layout_bucket
    )
    return {
        "id": actual_parcela.id if actual_parcela else None,
        "numero": int(item.get("numero") or 0),
        "referencia_mes": str(item["referencia_mes"]),
        "valor": str(item.get("valor") or "0.00"),
        "data_vencimento": str(item.get("data_vencimento") or item["referencia_mes"]),
        "status": str(item.get("status") or Parcela.Status.EM_PREVISAO),
        "data_pagamento": item.get("data_pagamento"),
        "observacao": str(item.get("observacao") or ""),
        "layout_bucket": resolved_bucket,
        "updated_at": (
            actual_parcela.updated_at.isoformat() if actual_parcela and actual_parcela.updated_at else None
        ),
        "financial_flags": _parcel_financial_flags(actual_parcela),
    }


def _resolve_editor_actual_parcela(
    item: dict[str, Any],
    *,
    current_parcelas_by_id: dict[int, Parcela],
    current_parcelas_by_ref: dict[date, list[Parcela]],
) -> Parcela | None:
    item_id = item.get("id")
    if item_id is not None:
        try:
            item_id_int = int(item_id)
        except (TypeError, ValueError):
            item_id_int = None
        if item_id_int is not None and item_id_int in current_parcelas_by_id:
            return current_parcelas_by_id[item_id_int]

    referencia_raw = item.get("referencia_mes")
    referencia = (
        referencia_raw
        if isinstance(referencia_raw, date)
        else _parse_optional_date(referencia_raw)
    )
    if referencia is None:
        return None
    candidates = current_parcelas_by_ref.get(referencia, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _active_operational_refinanciamento(contrato: Contrato) -> Refinanciamento | None:
    candidates = list(
        Refinanciamento.objects.filter(
            contrato_origem=contrato,
            deleted_at__isnull=True,
            legacy_refinanciamento_id__isnull=True,
        ).order_by("-competencia_solicitada", "-created_at", "-id")
    )
    if not candidates:
        return None

    priority_map = {
        Refinanciamento.Status.EFETIVADO: 100,
        Refinanciamento.Status.CONCLUIDO: 90,
        Refinanciamento.Status.APROVADO_PARA_RENOVACAO: 80,
        Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO: 70,
        Refinanciamento.Status.PENDENTE_TERMO_ANALISTA: 60,
        Refinanciamento.Status.EM_ANALISE_RENOVACAO: 50,
        Refinanciamento.Status.PENDENTE_TERMO_AGENTE: 40,
        Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO: 35,
        Refinanciamento.Status.BLOQUEADO: 30,
        Refinanciamento.Status.REVERTIDO: 25,
        Refinanciamento.Status.DESATIVADO: 20,
        Refinanciamento.Status.APTO_A_RENOVAR: 10,
    }

    def status_priority(item: Refinanciamento) -> tuple[int, Any, int]:
        marker = (
            item.executado_em
            or item.data_ativacao_ciclo
            or item.updated_at
            or item.created_at
        )
        return (priority_map.get(item.status, 0), marker, item.id)

    best_by_competencia: dict[date, Refinanciamento] = {}
    for item in candidates:
        competencia = item.competencia_solicitada.replace(day=1)
        current = best_by_competencia.get(competencia)
        if current is None or status_priority(item) > status_priority(current):
            best_by_competencia[competencia] = item

    ranked = sorted(
        best_by_competencia.values(),
        key=lambda item: (
            item.competencia_solicitada.replace(day=1),
            status_priority(item),
        ),
        reverse=True,
    )
    for item in ranked:
        if item.status in ACTIVE_OPERATIONAL_REFINANCIAMENTO_STATUSES:
            return item
    return None


def _resolve_contract_editor_flags(
    *,
    contrato: Contrato,
    projection: dict[str, Any],
    refinanciamento_ativo: Refinanciamento | None,
) -> dict[str, bool]:
    cycles = list(sorted(projection.get("cycles") or [], key=lambda item: item["numero"]))
    latest_cycle = cycles[-1] if cycles else None
    latest_cycle_status = str((latest_cycle or {}).get("status") or "")
    canonically_eligible = (
        latest_cycle is not None
        and latest_cycle_status not in CONCLUDED_CYCLE_STATUSES
        and is_contract_eligible_for_renewal_competencia(
            contrato,
            projection=projection,
        )
    )
    has_operational_renewal = (
        refinanciamento_ativo is not None
        and refinanciamento_ativo.status in ACTIVE_OPERATIONAL_REFINANCIAMENTO_STATUSES
    )
    return {
        "ciclo_ja_renovado": latest_cycle_status == Ciclo.Status.CICLO_RENOVADO,
        "contrato_nao_renovado": (
            contrato.status == Contrato.Status.ATIVO
            and latest_cycle is not None
            and not has_operational_renewal
            and not canonically_eligible
        ),
    }


def _manual_phase_slug(
    *,
    contrato: Contrato,
    cycle_status: str,
    is_latest_cycle: bool,
) -> str:
    associado_status = getattr(contrato.associado, "status", "")
    if associado_status == Associado.Status.INATIVO or contrato.status == Contrato.Status.CANCELADO:
        return "contrato_desativado"
    if contrato.status == Contrato.Status.ENCERRADO:
        return "contrato_encerrado"
    if cycle_status in CONCLUDED_CYCLE_STATUSES:
        return "ciclo_renovado"
    if is_latest_cycle:
        refinanciamento = _active_operational_refinanciamento(contrato)
        if refinanciamento is not None:
            mapping = {
                Refinanciamento.Status.APTO_A_RENOVAR: "apto_a_renovar",
                Refinanciamento.Status.EM_ANALISE_RENOVACAO: "renovacao_em_analise",
                Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO: "aguardando_coordenacao",
                Refinanciamento.Status.APROVADO_PARA_RENOVACAO: "aprovado_para_renovacao",
            }
            normalized = refinanciamento.status
            if normalized in mapping:
                return mapping[normalized]
    if cycle_status == Ciclo.Status.APTO_A_RENOVAR:
        return "apto_a_renovar"
    return "ciclo_aberto"


def _manual_financial_slug(
    *,
    contrato: Contrato,
    has_unpaid: bool,
) -> str:
    associado_status = str(getattr(contrato.associado, "status", "") or "")
    if associado_status == Associado.Status.INATIVO or contrato.status == Contrato.Status.CANCELADO:
        return "ciclo_desativado"
    if has_unpaid:
        return "ciclo_com_pendencia"
    return "ciclo_em_dia"


def _build_manual_contract_projection(
    contrato: Contrato,
    *,
    include_documents: bool = False,
) -> dict[str, Any]:
    ciclos = list(
        contrato.ciclos.filter(deleted_at__isnull=True).order_by("numero", "id")
    )
    parcelas = list(
        Parcela.all_objects.filter(ciclo__contrato=contrato, deleted_at__isnull=True)
        .exclude(status=Parcela.Status.CANCELADO)
        .select_related("ciclo")
        .order_by("ciclo__numero", "numero", "id")
    )
    by_cycle: dict[int, list[Parcela]] = defaultdict(list)
    unpaid_rows: list[dict[str, Any]] = []
    movement_rows: list[dict[str, Any]] = []

    for parcela in parcelas:
        unpaid_payload = {
            "id": parcela.id,
            "contrato_id": contrato.id,
            "contrato_codigo": contrato.codigo,
            "referencia_mes": parcela.referencia_mes,
            "valor": parcela.valor,
            "status": parcela.status,
            "data_pagamento": parcela.data_pagamento,
            "observacao": parcela.observacao,
            "source": "admin_override",
        }
        if parcela.layout_bucket == Parcela.LayoutBucket.UNPAID:
            unpaid_rows.append(unpaid_payload)
            continue
        if parcela.layout_bucket == Parcela.LayoutBucket.MOVEMENT:
            movement_rows.append(unpaid_payload)
            continue
        if parcela.status == Parcela.Status.NAO_DESCONTADO:
            unpaid_rows.append(
                {
                    **unpaid_payload,
                    "source": "admin_override_cycle",
                }
            )
        by_cycle[parcela.ciclo_id].append(parcela)

    unresolved_unpaid_rows = [
        item for item in unpaid_rows if _is_unresolved_unpaid_row(item)
    ]
    has_unpaid = bool(unresolved_unpaid_rows)
    projected_cycles: list[dict[str, Any]] = []
    latest_cycle_number = max((ciclo.numero for ciclo in ciclos), default=0)
    threshold = get_future_generation_threshold(contrato)
    refinanciamento_ativo = _active_operational_refinanciamento(contrato)
    for ciclo in ciclos:
        cycle_parcelas = by_cycle.get(ciclo.id, [])
        activation = get_cycle_activation_payload(ciclo)
        paid_count = sum(
            1 for parcela in cycle_parcelas if parcela.status == Parcela.Status.DESCONTADO
        )
        projected_cycle_status = ciclo.status
        if (
            ciclo.numero == latest_cycle_number
            and refinanciamento_ativo is None
            and paid_count >= threshold
            and projected_cycle_status not in CONCLUDED_CYCLE_STATUSES
        ):
            projected_cycle_status = Ciclo.Status.APTO_A_RENOVAR
        phase_slug = _manual_phase_slug(
            contrato=contrato,
            cycle_status=projected_cycle_status,
            is_latest_cycle=ciclo.numero == latest_cycle_number,
        )
        financial_slug = _manual_financial_slug(contrato=contrato, has_unpaid=has_unpaid)
        refinanciamento_operacional = (
            refinanciamento_ativo
            if ciclo.numero == latest_cycle_number
            else None
        )
        comprovantes_ciclo = []
        termo_antecipacao = None
        if include_documents:
            filtered = list(
                Comprovante.objects.filter(ciclo=ciclo, deleted_at__isnull=True)
                .order_by("created_at", "id")
            )
            termo = next(
                (item for item in filtered if item.tipo == Comprovante.Tipo.TERMO_ANTECIPACAO),
                None,
            )
            termo_antecipacao = _serialize_comprovante(termo) if termo else None
            comprovantes_ciclo = [
                _serialize_comprovante(item)
                for item in filtered
                if item.tipo != Comprovante.Tipo.TERMO_ANTECIPACAO
            ]
        projected_cycles.append(
            {
                "id": ciclo.id,
                "contrato_id": contrato.id,
                "contrato_codigo": contrato.codigo,
                "contrato_status": contrato.status,
                "numero": ciclo.numero,
                "data_inicio": ciclo.data_inicio,
                "data_fim": ciclo.data_fim,
                "status": projected_cycle_status,
                "fase_ciclo": phase_slug,
                "situacao_financeira": financial_slug,
                "status_visual_slug": phase_slug,
                "status_visual_label": STATUS_VISUAL_PHASE_LABELS[phase_slug],
                "valor_total": ciclo.valor_total,
                "data_ativacao_ciclo": activation["data_ativacao_ciclo"],
                "origem_data_ativacao": activation["origem_data_ativacao"],
                "ativacao_inferida": activation["ativacao_inferida"],
                "data_solicitacao_renovacao": activation["data_solicitacao_renovacao"],
                "data_renovacao": None,
                "origem_renovacao": "",
                "primeira_competencia_ciclo": (
                    cycle_parcelas[0].referencia_mes if cycle_parcelas else ciclo.data_inicio
                ),
                "ultima_competencia_ciclo": (
                    cycle_parcelas[-1].referencia_mes if cycle_parcelas else ciclo.data_fim
                ),
                "resumo_referencias": ", ".join(
                    parcela.referencia_mes.strftime("%m/%Y") for parcela in cycle_parcelas
                ),
                "refinanciamento_id": refinanciamento_operacional.id if refinanciamento_operacional else None,
                "legacy_refinanciamento_id": None,
                "comprovantes_ciclo": comprovantes_ciclo,
                "termo_antecipacao": termo_antecipacao,
                "parcelas": [
                    {
                        "id": parcela.id,
                        "numero": parcela.numero,
                        "referencia_mes": parcela.referencia_mes,
                        "valor": parcela.valor,
                        "data_vencimento": parcela.data_vencimento,
                        "status": parcela.status,
                        "data_pagamento": parcela.data_pagamento,
                        "observacao": parcela.observacao,
                    }
                    for parcela in cycle_parcelas
                ],
            }
        )

    status_renovacao = ""
    refinanciamento_id = None
    if refinanciamento_ativo is not None:
        status_renovacao = refinanciamento_ativo.status
        refinanciamento_id = refinanciamento_ativo.id
    elif projected_cycles and projected_cycles[-1]["status"] == Ciclo.Status.APTO_A_RENOVAR and is_contract_eligible_for_renewal_competencia(
        contrato,
        parcelas=list(projected_cycles[-1].get("parcelas") or []),
    ):
        status_renovacao = Refinanciamento.Status.APTO_A_RENOVAR

    return {
        "cycle_size": max(int(contrato.prazo_meses or 3), 1),
        "cycles": list(sorted(projected_cycles, key=lambda item: item["numero"], reverse=True)),
        "unpaid_months": sorted(unpaid_rows, key=lambda item: item["referencia_mes"], reverse=True),
        "possui_meses_nao_descontados": has_unpaid,
        "meses_nao_descontados_count": len(unresolved_unpaid_rows),
        "status_renovacao": status_renovacao,
        "refinanciamento_id": refinanciamento_id,
        "movimentos_financeiros_avulsos": sorted(
            movement_rows,
            key=lambda item: item["referencia_mes"],
        ),
    }


class AdminOverrideService:
    @staticmethod
    def _is_inactivation_event(event: AdminOverrideEvent) -> bool:
        after_meta = (event.after_snapshot or {}).get("meta") or {}
        before_meta = (event.before_snapshot or {}).get("meta") or {}
        return (
            str(after_meta.get("action") or before_meta.get("action") or "").strip()
            == "inativacao"
        )

    @staticmethod
    def latest_revertible_inactivation_event(
        associado: Associado,
    ) -> AdminOverrideEvent | None:
        events = (
            associado.admin_override_events.filter(
                escopo=AdminOverrideEvent.Scope.ASSOCIADO,
                revertida_em__isnull=True,
            )
            .select_related("realizado_por")
            .order_by("-created_at", "-id")
        )
        for event in events:
            if AdminOverrideService._is_inactivation_event(event):
                return event
        return None

    @staticmethod
    def build_legacy_inactivation_reversal_payload(
        associado: Associado,
    ) -> dict[str, Any]:
        automatic_event = AdminOverrideService.latest_revertible_inactivation_event(
            associado
        )
        current_status = str(associado.status or "")
        esteira = getattr(associado, "esteira_item", None)
        suggested_stage = (
            esteira.etapa_atual
            if esteira is not None and esteira.etapa_atual != EsteiraItem.Etapa.CONCLUIDO
            else EsteiraItem.Etapa.ANALISE
        )
        suggested_queue_status = (
            esteira.status
            if esteira is not None
            and esteira.etapa_atual != EsteiraItem.Etapa.CONCLUIDO
            and esteira.status in LEGACY_INACTIVATION_RETURN_QUEUE_STATUSES
            else EsteiraItem.Situacao.AGUARDANDO
        )
        return {
            "available": automatic_event is None
            and current_status in LEGACY_INACTIVATION_SOURCE_STATUSES,
            "current_status": current_status,
            "suggested_status": Associado.Status.ATIVO,
            "suggested_esteira_etapa": suggested_stage,
            "suggested_esteira_status": suggested_queue_status,
        }

    @staticmethod
    def build_associado_history_payload(associado: Associado, *, request=None) -> list[dict[str, Any]]:
        events = (
            AdminOverrideEvent.objects.filter(associado=associado)
            .select_related(
                "realizado_por",
                "revertida_por",
                "contrato",
                "ciclo",
                "parcela",
                "refinanciamento",
                "documento",
                "comprovante",
            )
            .prefetch_related("changes")
            .order_by("-created_at", "-id")
        )
        payload: list[dict[str, Any]] = []
        for event in events:
            payload.append(
                {
                    "id": event.id,
                    "escopo": event.escopo,
                    "resumo": event.resumo,
                    "motivo": event.motivo,
                    "confirmacao_dupla": event.confirmacao_dupla,
                    "created_at": event.created_at,
                    "realizado_por": {
                        "id": event.realizado_por_id,
                        "full_name": event.realizado_por.full_name,
                    },
                    "revertida_em": event.revertida_em,
                    "revertida_por": (
                        {
                            "id": event.revertida_por_id,
                            "full_name": event.revertida_por.full_name,
                        }
                        if event.revertida_por_id
                        else None
                    ),
                    "motivo_reversao": event.motivo_reversao,
                    "before_snapshot": event.before_snapshot,
                    "after_snapshot": event.after_snapshot,
                    "changes": [
                        {
                            "id": change.id,
                            "entity_type": change.entity_type,
                            "entity_id": change.entity_id,
                            "competencia_referencia": change.competencia_referencia,
                            "resumo": change.resumo,
                            "before_snapshot": change.before_snapshot,
                            "after_snapshot": change.after_snapshot,
                        }
                        for change in event.changes.all()
                    ],
                }
            )
        return payload

    @staticmethod
    def _visible_admin_editor_contracts(associado: Associado) -> list[Contrato]:
        contratos = get_operational_contracts_for_associado(associado)
        if contratos or associado.status != Associado.Status.INATIVO:
            return list(
                sorted(
                    contratos,
                    key=lambda contrato: (contrato.created_at, contrato.id),
                    reverse=True,
                )
            )

        cached = getattr(associado, "_prefetched_objects_cache", {}).get("contratos")
        if cached is not None:
            historicos = [
                contrato
                for contrato in cached
                if contrato.deleted_at is None and contrato.contrato_canonico_id is None
            ]
            if historicos:
                return list(
                    sorted(
                        historicos,
                        key=lambda contrato: (contrato.created_at, contrato.id),
                        reverse=True,
                    )
                )

        # Fallback final: inclui contratos soft-deletados (inativacoes legadas)
        return list(
            Contrato.all_objects.filter(
                associado=associado,
                contrato_canonico__isnull=True,
            )
            .select_related("agente")
            .order_by("-created_at", "-id")
        )

    @staticmethod
    def build_associado_editor_payload(
        associado: Associado,
        *,
        extra_warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        contratos = AdminOverrideService._visible_admin_editor_contracts(associado)
        payload_contracts: list[dict[str, Any]] = []
        payload_warnings: list[dict[str, Any]] = list(extra_warnings or [])
        for contrato in contratos:
            refinanciamento_ativo = _active_operational_refinanciamento(contrato)
            projection = AdminOverrideService.build_contract_projection_for_response(
                contrato,
                include_documents=True,
            )
            editor_flags = _resolve_contract_editor_flags(
                contrato=contrato,
                projection=projection,
                refinanciamento_ativo=refinanciamento_ativo,
            )
            # Para contratos soft-deletados (inativacao legada), inclui ciclos e parcelas excluidos
            _legado_deletado = contrato.deleted_at is not None
            _ciclos_qs = (
                Ciclo.all_objects.filter(contrato=contrato)
                if _legado_deletado
                else contrato.ciclos.filter(deleted_at__isnull=True)
            )
            current_cycles = {
                ciclo.numero: ciclo
                for ciclo in _ciclos_qs.order_by("numero", "id")
            }
            current_parcelas_by_id: dict[int, Parcela] = {}
            current_parcelas_by_ref: dict[date, list[Parcela]] = defaultdict(list)
            _parcelas_filter = (
                Parcela.all_objects.filter(ciclo__contrato=contrato)
                if _legado_deletado
                else Parcela.all_objects.filter(ciclo__contrato=contrato, deleted_at__isnull=True)
            )
            for parcela in (
                _parcelas_filter
                .exclude(status=Parcela.Status.CANCELADO)
                .order_by("updated_at", "id")
            ):
                current_parcelas_by_id[parcela.id] = parcela
                current_parcelas_by_ref[parcela.referencia_mes].append(parcela)

            cycles_payload = []
            projected_cycle_numbers: set[int] = set()
            for cycle in sorted(projection["cycles"], key=lambda item: item["numero"]):
                projected_cycle_numbers.add(int(cycle["numero"]))
                actual_cycle = current_cycles.get(int(cycle["numero"]))
                cycles_payload.append(
                    {
                        "id": actual_cycle.id if actual_cycle else None,
                        "numero": cycle["numero"],
                        "data_inicio": str(cycle["data_inicio"]),
                        "data_fim": str(cycle["data_fim"]),
                        "status": cycle["status"],
                        "valor_total": str(cycle["valor_total"]),
                        "updated_at": (
                            actual_cycle.updated_at.isoformat()
                            if actual_cycle and actual_cycle.updated_at
                            else None
                        ),
                        "comprovantes_ciclo": cycle.get("comprovantes_ciclo", []),
                        "termo_antecipacao": cycle.get("termo_antecipacao"),
                        "parcelas": [
                            _build_editor_item(
                                parcela,
                                actual_parcela=_resolve_editor_actual_parcela(
                                    parcela,
                                    current_parcelas_by_id=current_parcelas_by_id,
                                    current_parcelas_by_ref=current_parcelas_by_ref,
                                ),
                                bucket=Parcela.LayoutBucket.CYCLE,
                            )
                            for parcela in cycle["parcelas"]
                        ],
                    }
                )
            for actual_cycle in sorted(
                current_cycles.values(),
                key=lambda item: (item.numero, item.id),
            ):
                if actual_cycle.numero in projected_cycle_numbers:
                    continue
                cycles_payload.append(
                    {
                        "id": actual_cycle.id,
                        "numero": actual_cycle.numero,
                        "data_inicio": actual_cycle.data_inicio.isoformat(),
                        "data_fim": actual_cycle.data_fim.isoformat(),
                        "status": actual_cycle.status,
                        "valor_total": str(actual_cycle.valor_total),
                        "updated_at": (
                            actual_cycle.updated_at.isoformat()
                            if actual_cycle.updated_at
                            else None
                        ),
                        "comprovantes_ciclo": [],
                        "termo_antecipacao": None,
                        "parcelas": [],
                    }
                )
            cycles_payload.sort(
                key=lambda item: (int(item["numero"]), int(item["id"] or 0))
            )

            unpaid_payload = [
                _build_editor_item(
                    item,
                    actual_parcela=_resolve_editor_actual_parcela(
                        item,
                        current_parcelas_by_id=current_parcelas_by_id,
                        current_parcelas_by_ref=current_parcelas_by_ref,
                    ),
                    bucket=Parcela.LayoutBucket.UNPAID,
                )
                for item in projection["unpaid_months"]
            ]
            movement_payload = [
                _build_editor_item(
                    item,
                    actual_parcela=_resolve_editor_actual_parcela(
                        item,
                        current_parcelas_by_id=current_parcelas_by_id,
                        current_parcelas_by_ref=current_parcelas_by_ref,
                    ),
                    bucket=Parcela.LayoutBucket.MOVEMENT,
                )
                for item in projection["movimentos_financeiros_avulsos"]
            ]
            payload_contracts.append(
                {
                    "id": contrato.id,
                    "updated_at": contrato.updated_at.isoformat() if contrato.updated_at else None,
                    "codigo": contrato.codigo,
                    "status": contrato.status,
                    "valor_bruto": str(contrato.valor_bruto),
                    "valor_liquido": str(contrato.valor_liquido),
                    "valor_mensalidade": str(contrato.valor_mensalidade),
                    "prazo_meses": contrato.prazo_meses,
                    "taxa_antecipacao": str(contrato.taxa_antecipacao),
                    "margem_disponivel": str(contrato.margem_disponivel),
                    "valor_total_antecipacao": str(contrato.valor_total_antecipacao),
                    "doacao_associado": str(contrato.doacao_associado),
                    "comissao_agente": str(contrato.comissao_agente),
                    "data_contrato": (
                        contrato.data_contrato.isoformat() if contrato.data_contrato else None
                    ),
                    "data_aprovacao": contrato.data_aprovacao.isoformat() if contrato.data_aprovacao else None,
                    "data_primeira_mensalidade": (
                        contrato.data_primeira_mensalidade.isoformat()
                        if contrato.data_primeira_mensalidade
                        else None
                    ),
                    "mes_averbacao": contrato.mes_averbacao.isoformat() if contrato.mes_averbacao else None,
                    "auxilio_liberado_em": (
                        contrato.auxilio_liberado_em.isoformat()
                        if contrato.auxilio_liberado_em
                        else None
                    ),
                    "ciclo_ja_renovado": editor_flags["ciclo_ja_renovado"],
                    "contrato_nao_renovado": editor_flags["contrato_nao_renovado"],
                    "ciclos": cycles_payload,
                    "meses_nao_pagos": unpaid_payload,
                    "movimentos_financeiros_avulsos": movement_payload,
                    "refinanciamento_ativo": (
                        _serialize_refinanciamento_for_editor(
                            refinanciamento_ativo,
                            contrato,
                        )
                        if refinanciamento_ativo
                        else None
                    ),
                }
            )
            payload_warnings.extend(_build_contract_layout_warnings(contrato))
            payload_warnings.extend(
                _build_renewal_queue_warnings(
                    contrato,
                    projection=projection,
                    refinanciamento_ativo=refinanciamento_ativo,
                )
            )
        inactivation_event = AdminOverrideService.latest_revertible_inactivation_event(
            associado
        )
        legacy_inactivation_reversal = (
            AdminOverrideService.build_legacy_inactivation_reversal_payload(associado)
        )
        return {
            "associado": _serialize_associado(associado),
            "contratos": payload_contracts,
            "esteira": _serialize_esteira(getattr(associado, "esteira_item", None)),
            "documentos": [
                _serialize_documento(documento)
                for documento in associado.documentos.filter(deleted_at__isnull=True).order_by("tipo", "id")
            ],
            "inactivation_reversal": (
                {
                    "event_id": inactivation_event.id,
                    "available": True,
                    "previous_status": str(
                        (
                            (inactivation_event.before_snapshot or {}).get("meta") or {}
                        ).get("previous_status")
                        or (
                            (inactivation_event.before_snapshot or {}).get("associado")
                            or {}
                        ).get("status")
                        or ""
                    ),
                    "target_status": str(
                        (
                            (inactivation_event.after_snapshot or {}).get("meta") or {}
                        ).get("target_status")
                        or (
                            (inactivation_event.after_snapshot or {}).get("associado")
                            or {}
                        ).get("status")
                        or ""
                    ),
                    "event_created_at": (
                        inactivation_event.created_at.isoformat()
                        if inactivation_event and inactivation_event.created_at
                        else None
                    ),
                    "realizado_por": (
                        {
                            "id": inactivation_event.realizado_por.id,
                            "full_name": inactivation_event.realizado_por.full_name,
                        }
                        if inactivation_event and inactivation_event.realizado_por_id
                        else None
                    ),
                }
                if inactivation_event is not None
                else {
                    "event_id": None,
                    "available": False,
                    "previous_status": "",
                    "target_status": "",
                    "event_created_at": None,
                    "realizado_por": None,
                }
            ),
            "legacy_inactivation_reversal": legacy_inactivation_reversal,
            "warnings": _dedupe_warning_list(payload_warnings),
        }

    @staticmethod
    def apply_save_all(
        *,
        associado: Associado,
        payload: dict[str, Any],
        user,
    ) -> dict[str, Any]:
        motivo = str(payload.get("motivo") or "").strip()
        if not motivo:
            raise ValidationError("O motivo da alteração é obrigatório.")

        contratos_payload = payload.get("contratos") or []
        esteira_payload = payload.get("esteira")
        if not contratos_payload and not esteira_payload:
            raise ValidationError("Nenhuma alteração pendente foi enviada para salvar.")
        root_dirty_sections = {
            str(section).strip()
            for section in (payload.get("dirty_sections") or [])
            if str(section).strip()
        }
        operation_warnings: list[dict[str, Any]] = []

        visible_contract_ids = [
            contrato.id
            for contrato in AdminOverrideService._visible_admin_editor_contracts(associado)
        ]
        contratos_by_id = {
            contrato.id: contrato
            for contrato in Contrato.all_objects.filter(id__in=visible_contract_ids)
            .select_related("agente")
            .prefetch_related("ciclos__parcelas")
        }
        refinanciamentos_by_id = {
            refinanciamento.id: refinanciamento
            for refinanciamento in Refinanciamento.objects.filter(
                associado=associado,
                deleted_at__isnull=True,
            ).select_related("contrato_origem")
        }
        touched_contract_ids: set[int] = set()
        manual_cycle_layout_touched_contract_ids: set[int] = set()

        with transaction.atomic():
            for contract_payload in contratos_payload:
                contrato = contratos_by_id.get(contract_payload["id"])
                if contrato is None:
                    raise ValidationError("Contrato inválido para o associado informado.")
                explicit_contract_sections = "dirty_sections" in contract_payload
                contract_dirty_sections = {
                    str(section).strip()
                    for section in (contract_payload.get("dirty_sections") or [])
                    if str(section).strip()
                }

                core_payload = contract_payload.get("core")
                if (
                    not explicit_contract_sections
                    and core_payload
                    and _contract_core_payload_has_effective_changes(contrato, core_payload)
                ):
                    contract_dirty_sections.add("contract_core")
                if core_payload and "contract_core" in contract_dirty_sections:
                    contrato = AdminOverrideService.apply_contract_core_override(
                        contrato=contrato,
                        payload={**core_payload, "motivo": motivo},
                        user=user,
                        wrap_atomic=False,
                    )
                    contratos_by_id[contrato.id] = contrato
                    touched_contract_ids.add(contrato.id)

                cycles_payload = contract_payload.get("cycles")
                if (
                    not explicit_contract_sections
                    and cycles_payload
                    and _cycle_layout_payload_has_effective_changes(contrato, cycles_payload)
                ):
                    contract_dirty_sections.add("cycle_layout")
                if cycles_payload and "cycle_layout" in contract_dirty_sections:
                    # Restaurar arvore completa (inativacao legada com contrato soft-deletado)
                    if contrato.deleted_at is not None:
                        now = timezone.now()
                        Parcela.all_objects.filter(
                            ciclo__contrato=contrato, deleted_at__isnull=False
                        ).update(deleted_at=None, updated_at=now)
                        Ciclo.all_objects.filter(
                            contrato=contrato, deleted_at__isnull=False
                        ).update(deleted_at=None, updated_at=now)
                        Contrato.all_objects.filter(pk=contrato.pk).update(
                            deleted_at=None, updated_at=now
                        )
                        contrato.refresh_from_db()
                        contratos_by_id[contrato.id] = contrato
                    cycle_rows = list(cycles_payload.get("cycles") or [])
                    parcela_rows = list(cycles_payload.get("parcelas") or [])
                    if not cycle_rows and not parcela_rows:
                        raise ValidationError({"detail": "Informe ao menos um ciclo no layout."})
                    cycles_updated_at = (
                        contrato.updated_at.isoformat() if getattr(contrato, "updated_at", None) else None
                    )
                    contrato, cycle_warnings = AdminOverrideService.apply_cycle_layout_override(
                        contrato=contrato,
                        payload={
                            **cycles_payload,
                            "updated_at": cycles_updated_at or cycles_payload.get("updated_at"),
                            "motivo": motivo,
                        },
                        user=user,
                        wrap_atomic=False,
                        sync_associado_status=False,
                    )
                    operation_warnings.extend(cycle_warnings)
                    contratos_by_id[contrato.id] = contrato
                    touched_contract_ids.add(contrato.id)
                    manual_cycle_layout_touched_contract_ids.add(contrato.id)

                refinanciamento_payload = contract_payload.get("refinanciamento")
                refinanciamento = None
                refinanciamento_has_effective_changes = False
                if refinanciamento_payload:
                    refinanciamento = refinanciamentos_by_id.get(refinanciamento_payload["id"])
                    if refinanciamento is not None and refinanciamento.contrato_origem_id == contrato.id:
                        refinanciamento_payload = _sanitize_save_all_refinanciamento_payload(
                            refinanciamento=refinanciamento,
                            contrato=contrato,
                            payload=refinanciamento_payload,
                            warnings=operation_warnings,
                        )
                        refinanciamento_has_effective_changes = (
                            _refinanciamento_payload_has_effective_changes(
                                refinanciamento,
                                refinanciamento_payload,
                            )
                        )
                if (
                    not explicit_contract_sections
                    and refinanciamento_payload
                    and refinanciamento_has_effective_changes
                ):
                    contract_dirty_sections.add("refinanciamento")
                if (
                    refinanciamento_payload
                    and "refinanciamento" in contract_dirty_sections
                    and refinanciamento_has_effective_changes
                ):
                    if refinanciamento is None or refinanciamento.contrato_origem_id != contrato.id:
                        raise ValidationError("Renovação inválida para o contrato informado.")
                    AdminOverrideService.apply_refinanciamento_override(
                        refinanciamento=refinanciamento,
                        payload={**refinanciamento_payload, "motivo": motivo},
                        user=user,
                        wrap_atomic=False,
                        sync_associado_status=False,
                    )
                    touched_contract_ids.add(contrato.id)

            for contrato_id in sorted(touched_contract_ids):
                contrato = contratos_by_id[contrato_id]
                contrato.refresh_from_db()
                if (
                    contrato_id in manual_cycle_layout_touched_contract_ids
                    and contrato.admin_manual_layout_enabled
                ):
                    contratos_by_id[contrato_id] = contrato
                    continue
                rebuild_contract_cycle_state(contrato, execute=True)
                contratos_by_id[contrato_id] = contrato

            if (
                "dirty_sections" not in payload
                and esteira_payload
                and _esteira_payload_has_effective_changes(associado, esteira_payload)
            ):
                root_dirty_sections.add("esteira")
            if esteira_payload and "esteira" in root_dirty_sections:
                AdminOverrideService.apply_esteira_override(
                    associado=associado,
                    payload={**esteira_payload, "motivo": motivo},
                    user=user,
                    wrap_atomic=False,
                )
            _normalize_esteira_conclusion_state(getattr(associado, "esteira_item", None))

        sync_associado_mother_status(associado)
        associado.refresh_from_db()
        return AdminOverrideService.build_associado_editor_payload(
            associado,
            extra_warnings=operation_warnings,
        )

    @staticmethod
    def apply_legacy_inactivation_reversal(
        *,
        associado: Associado,
        payload: dict[str, Any],
        user,
    ) -> dict[str, Any]:
        motivo = str(payload.get("motivo") or "").strip()
        if not motivo:
            raise ValidationError("O motivo da reversão é obrigatório.")
        if str(associado.status or "") not in LEGACY_INACTIVATION_SOURCE_STATUSES:
            raise ValidationError(
                "A reversão assistida legada só está disponível para associados inativados."
            )
        if AdminOverrideService.latest_revertible_inactivation_event(associado) is not None:
            raise ValidationError(
                "Este associado já possui uma reversão automática disponível. Use a reversão da inativação registrada."
            )

        restored_status = str(payload.get("status_retorno") or "").strip()
        target_stage = str(payload.get("etapa_esteira") or "").strip()
        target_queue_status = str(payload.get("status_esteira") or "").strip()
        if restored_status not in LEGACY_INACTIVATION_RETURN_STATUSES:
            raise ValidationError("Escolha um status de retorno válido.")
        if target_stage == EsteiraItem.Etapa.CONCLUIDO:
            raise ValidationError("A esteira precisa voltar para uma etapa operacional aberta.")
        if target_queue_status not in LEGACY_INACTIVATION_RETURN_QUEUE_STATUSES:
            raise ValidationError("Escolha um status operacional válido para a esteira.")
        esteira_note = str(payload.get("observacao_esteira") or "").strip()

        with transaction.atomic():
            before_associado = _serialize_associado(associado)
            before_esteira = _serialize_esteira(getattr(associado, "esteira_item", None))

            associado.status = restored_status
            associado.save(update_fields=["status", "updated_at"])

            esteira = getattr(associado, "esteira_item", None)
            previous_stage = (
                esteira.etapa_atual
                if esteira is not None
                else EsteiraItem.Etapa.CONCLUIDO
            )
            previous_queue_status = (
                esteira.status
                if esteira is not None
                else EsteiraItem.Situacao.REJEITADO
            )
            prioridade = (
                int(esteira.prioridade)
                if esteira is not None and esteira.prioridade
                else 3
            )

            if esteira is None:
                esteira = EsteiraItem.objects.create(
                    associado=associado,
                    etapa_atual=target_stage,
                    status=target_queue_status,
                    prioridade=prioridade,
                    observacao=(
                        esteira_note
                        or "Reaberto por reversão assistida de inativação legada."
                    ),
                )
            else:
                esteira.etapa_atual = target_stage
                esteira.status = target_queue_status
                esteira.prioridade = prioridade
                esteira.observacao = (
                    esteira_note
                    or "Reaberto por reversão assistida de inativação legada."
                )
                esteira.assumido_em = None
                esteira.heartbeat_at = None
                esteira.analista_responsavel_id = None
                esteira.coordenador_responsavel_id = None
                esteira.tesoureiro_responsavel_id = None
                esteira.save()

            _normalize_esteira_conclusion_state(esteira)
            Transicao.objects.create(
                esteira_item=esteira,
                acao="reverter_inativacao_legada",
                de_status=previous_stage,
                para_status=esteira.etapa_atual,
                de_situacao=previous_queue_status,
                para_situacao=esteira.status,
                realizado_por=user,
                observacao=motivo,
            )

            after_associado = _serialize_associado(associado)
            after_esteira = _serialize_esteira(esteira)
            AdminOverrideService._record_event(
                context=_OverrideContext(associado=associado),
                user=user,
                escopo=AdminOverrideEvent.Scope.ASSOCIADO,
                resumo="Reversão assistida de inativação legada",
                motivo=motivo,
                before_snapshot={
                    "meta": {
                        "action": "reversao_inativacao_legada",
                        "current_status": before_associado.get("status"),
                        "restored_status": after_associado.get("status"),
                    },
                    "associado": before_associado,
                    "esteira": before_esteira,
                },
                after_snapshot={
                    "meta": {
                        "action": "reversao_inativacao_legada",
                        "current_status": before_associado.get("status"),
                        "restored_status": after_associado.get("status"),
                    },
                    "associado": after_associado,
                    "esteira": after_esteira,
                },
                changes=[
                    {
                        "entity_type": AdminOverrideChange.EntityType.ASSOCIADO,
                        "entity_id": associado.id,
                        "resumo": "Associado reaberto por reversão assistida legada",
                        "before_snapshot": before_associado,
                        "after_snapshot": after_associado,
                    },
                    {
                        "entity_type": AdminOverrideChange.EntityType.ESTEIRA,
                        "entity_id": esteira.id,
                        "resumo": "Esteira operacional reaberta após reversão assistida legada",
                        "before_snapshot": before_esteira,
                        "after_snapshot": after_esteira,
                    },
                ],
            )

        associado.refresh_from_db()
        return AdminOverrideService.build_associado_editor_payload(associado)

    @staticmethod
    def _resolve_contract_for_safe_transition(
        *,
        associado: Associado,
        contrato_id: int,
    ) -> Contrato:
        try:
            return associado.contratos.exclude(status=Contrato.Status.CANCELADO).get(
                id=contrato_id
            )
        except Contrato.DoesNotExist as exc:
            raise ValidationError("Contrato inválido para a transição administrativa.") from exc

    @staticmethod
    def _upsert_refinanciamento_items_from_cycle(
        *,
        refinanciamento: Refinanciamento,
        parcelas: list[dict[str, Any]],
    ) -> None:
        paid_parcelas = [
            parcela for parcela in parcelas if _is_paid_projection_status(parcela.get("status"))
        ]
        referencias_pagas = [
            _parse_optional_date(parcela.get("referencia_mes"))
            for parcela in paid_parcelas
        ]
        referencias_pagas = [referencia for referencia in referencias_pagas if referencia is not None]
        Item.objects.filter(refinanciamento=refinanciamento).exclude(
            referencia_month__in=referencias_pagas
        ).delete()

        for parcela in paid_parcelas:
            referencia = _parse_optional_date(parcela.get("referencia_mes"))
            if referencia is None:
                continue
            Item.objects.update_or_create(
                refinanciamento=refinanciamento,
                referencia_month=referencia,
                defaults={
                    "pagamento_mensalidade": None,
                    "status_code": "1",
                    "valor": _parse_decimal(parcela.get("valor")),
                    "import_uuid": "",
                    "source_file_path": "",
                },
            )

    @staticmethod
    def _ensure_refinanciamento_assumption(
        *,
        refinanciamento: Refinanciamento,
        referencias: list[date],
        user,
    ) -> Assumption | None:
        if not refinanciamento.cycle_key:
            return None
        now = timezone.now()
        assumption, _created = Assumption.objects.update_or_create(
            cadastro=refinanciamento.associado,
            request_key=refinanciamento.cycle_key,
            defaults={
                "cpf_cnpj": refinanciamento.associado.cpf_cnpj,
                "refs_json": [referencia.isoformat() for referencia in referencias],
                "solicitado_por": refinanciamento.solicitado_por or user,
                "status": Assumption.Status.LIBERADO,
                "solicitado_em": refinanciamento.created_at or now,
                "liberado_em": now,
                "assumido_em": None,
                "finalizado_em": None,
                "analista": None,
                "heartbeat_at": None,
            },
        )
        return assumption

    @staticmethod
    def _materialize_safe_transition_refinanciamento(
        *,
        contrato: Contrato,
        projection: dict[str, Any],
        user,
    ) -> Refinanciamento:
        current_cycle = _latest_projection_cycle(projection)
        if current_cycle is None:
            raise ValidationError(
                "O contrato não possui ciclo projetado para materializar a fila operacional."
            )

        current_ciclo = (
            contrato.ciclos.filter(
                deleted_at__isnull=True,
                numero=int(current_cycle["numero"]),
            )
            .order_by("id")
            .first()
        )
        if current_ciclo is None:
            raise ValidationError(
                "O ciclo operacional de referência não foi encontrado para este contrato."
            )

        referencias = [
            _parse_optional_date(parcela.get("referencia_mes"))
            for parcela in list(current_cycle.get("parcelas") or [])
        ]
        referencias = [referencia for referencia in referencias if referencia is not None]
        if not referencias:
            fallback_referencia = (
                _parse_optional_date(current_cycle.get("data_fim"))
                or resolve_current_renewal_competencia()
            )
            referencias = [fallback_referencia]

        cycle_key = "|".join(referencia.strftime("%Y-%m") for referencia in referencias)
        competencia_solicitada = max(referencias).replace(day=1)
        parcelas = list(current_cycle.get("parcelas") or [])
        paid_count = sum(
            1 for parcela in parcelas if _is_paid_projection_status(parcela.get("status"))
        )

        reusable = (
            Refinanciamento.objects.filter(
                contrato_origem=contrato,
                deleted_at__isnull=True,
                legacy_refinanciamento_id__isnull=True,
                origem=Refinanciamento.Origem.OPERACIONAL,
                ciclo_destino__isnull=True,
                executado_em__isnull=True,
                data_ativacao_ciclo__isnull=True,
            )
            .filter(cycle_key=cycle_key)
            .order_by("-created_at", "-id")
            .first()
        )
        if reusable is None:
            reusable = (
                Refinanciamento.objects.filter(
                    contrato_origem=contrato,
                    deleted_at__isnull=True,
                    legacy_refinanciamento_id__isnull=True,
                    origem=Refinanciamento.Origem.OPERACIONAL,
                    ciclo_destino__isnull=True,
                    executado_em__isnull=True,
                    data_ativacao_ciclo__isnull=True,
                    competencia_solicitada=competencia_solicitada,
                )
                .order_by("-created_at", "-id")
                .first()
            )

        defaults = {
            "associado": contrato.associado,
            "contrato_origem": contrato,
            "solicitado_por": user,
            "competencia_solicitada": competencia_solicitada,
            "status": Refinanciamento.Status.PENDENTE_APTO,
            "ciclo_origem": current_ciclo,
            "ciclo_destino": None,
            "valor_refinanciamento": contrato.valor_liquido or contrato.valor_total_antecipacao,
            "repasse_agente": contrato.comissao_agente,
            "mode": "admin_safe",
            "origem": Refinanciamento.Origem.OPERACIONAL,
            "cycle_key": cycle_key,
            "ref1": referencias[0] if referencias else None,
            "ref2": referencias[1] if len(referencias) > 1 else None,
            "ref3": referencias[2] if len(referencias) > 2 else None,
            "ref4": referencias[3] if len(referencias) > 3 else None,
            "cpf_cnpj_snapshot": contrato.associado.cpf_cnpj,
            "nome_snapshot": contrato.associado.nome_completo,
            "agente_snapshot": contrato.agente.full_name if contrato.agente else "",
            "contrato_codigo_origem": contrato.codigo,
            "contrato_codigo_novo": "",
            "parcelas_ok": paid_count,
            "parcelas_json": [
                {
                    "referencia_month": referencia.isoformat(),
                    "status_code": "1",
                    "valor": str(contrato.valor_mensalidade),
                }
                for referencia in referencias[:paid_count]
            ],
            "data_ativacao_ciclo": None,
            "executado_em": None,
            "motivo_bloqueio": "",
            "observacao": "",
            "analista_note": "",
            "coordenador_note": "",
            "reviewed_by": None,
            "reviewed_at": None,
            "aprovado_por": None,
            "bloqueado_por": None,
            "efetivado_por": None,
            "termo_antecipacao_path": "",
            "termo_antecipacao_original_name": "",
            "termo_antecipacao_mime": "",
            "termo_antecipacao_size_bytes": None,
            "termo_antecipacao_uploaded_at": None,
        }

        if reusable is None:
            refinanciamento = Refinanciamento.objects.create(**defaults)
        else:
            refinanciamento = reusable
            changed_fields: list[str] = []
            for field, value in defaults.items():
                if getattr(refinanciamento, field) != value:
                    setattr(refinanciamento, field, value)
                    changed_fields.append(field)
            if changed_fields:
                refinanciamento.save(update_fields=[*changed_fields, "updated_at"])

        duplicates = (
            Refinanciamento.objects.filter(
                contrato_origem=contrato,
                deleted_at__isnull=True,
                legacy_refinanciamento_id__isnull=True,
                origem=Refinanciamento.Origem.OPERACIONAL,
                ciclo_destino__isnull=True,
                executado_em__isnull=True,
                data_ativacao_ciclo__isnull=True,
                cycle_key=cycle_key,
            )
            .exclude(pk=refinanciamento.pk)
            .order_by("-created_at", "-id")
        )
        for duplicate in duplicates:
            duplicate.soft_delete()

        AdminOverrideService._upsert_refinanciamento_items_from_cycle(
            refinanciamento=refinanciamento,
            parcelas=parcelas,
        )
        AdminOverrideService._ensure_refinanciamento_assumption(
            refinanciamento=refinanciamento,
            referencias=referencias,
            user=user,
        )
        return refinanciamento

    @staticmethod
    def _transition_renewal_esteira(
        *,
        refinanciamento: Refinanciamento,
        target_status: str,
        motivo: str,
        user,
    ) -> None:
        esteira = (
            getattr(refinanciamento.associado, "esteira_item", None)
            if refinanciamento.associado_id
            else None
        )
        if esteira is None:
            return

        previous_stage = esteira.etapa_atual
        previous_status = esteira.status

        if target_status == Refinanciamento.Status.EFETIVADO:
            esteira.etapa_atual = EsteiraItem.Etapa.CONCLUIDO
            esteira.status = EsteiraItem.Situacao.APROVADO
        elif target_status == Refinanciamento.Status.REVERTIDO:
            esteira.etapa_atual = EsteiraItem.Etapa.CONCLUIDO
            esteira.status = EsteiraItem.Situacao.REJEITADO
        elif target_status == Refinanciamento.Status.APTO_A_RENOVAR:
            esteira.etapa_atual = EsteiraItem.Etapa.ANALISE
            esteira.status = EsteiraItem.Situacao.AGUARDANDO
        elif target_status in {
            Refinanciamento.Status.EM_ANALISE_RENOVACAO,
            Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
            Refinanciamento.Status.PENDENTE_TERMO_AGENTE,
            Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
        }:
            esteira.etapa_atual = EsteiraItem.Etapa.ANALISE
            esteira.status = (
                EsteiraItem.Situacao.AGUARDANDO
                if target_status
                in {
                    Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
                    Refinanciamento.Status.PENDENTE_TERMO_AGENTE,
                }
                else EsteiraItem.Situacao.EM_ANDAMENTO
            )
        else:
            return

        if previous_stage == esteira.etapa_atual and previous_status == esteira.status:
            _normalize_esteira_conclusion_state(
                esteira,
                explicit_transition=esteira.etapa_atual == EsteiraItem.Etapa.CONCLUIDO,
            )
            return

        esteira.save(update_fields=["etapa_atual", "status", "updated_at"])
        _normalize_esteira_conclusion_state(
            esteira,
            explicit_transition=esteira.etapa_atual == EsteiraItem.Etapa.CONCLUIDO,
        )
        Transicao.objects.create(
            esteira_item=esteira,
            acao="admin_safe_renewal_stage_transition",
            de_status=previous_stage,
            para_status=esteira.etapa_atual,
            de_situacao=previous_status,
            para_situacao=esteira.status,
            realizado_por=user,
            observacao=motivo,
        )

    @staticmethod
    def _finalize_refinanciamento_assumption(
        *,
        refinanciamento: Refinanciamento,
        finalized: bool,
        user,
    ) -> None:
        if not refinanciamento.cycle_key:
            return
        assumption = (
            Assumption.objects.filter(
                cadastro=refinanciamento.associado,
                request_key=refinanciamento.cycle_key,
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if assumption is None:
            return

        update_fields = [
            "status",
            "analista",
            "assumido_em",
            "heartbeat_at",
            "finalizado_em",
            "updated_at",
        ]
        assumption.analista = assumption.analista or user
        assumption.assumido_em = None
        assumption.heartbeat_at = None
        if finalized:
            assumption.status = Assumption.Status.FINALIZADO
            assumption.finalizado_em = timezone.now()
        else:
            assumption.status = Assumption.Status.LIBERADO
            assumption.finalizado_em = None
            assumption.liberado_em = timezone.now()
            update_fields.append("liberado_em")
        assumption.save(update_fields=update_fields)

    @staticmethod
    def _effectivate_refinanciamento_from_existing_cycles(
        *,
        refinanciamento: Refinanciamento,
        motivo: str,
        user,
    ) -> Refinanciamento:
        contrato = refinanciamento.contrato_origem
        if contrato is None:
            raise ValidationError("Renovação sem contrato de origem.")

        ciclo_origem = refinanciamento.ciclo_origem
        if ciclo_origem is None:
            raise ValidationError(
                "A renovação não possui ciclo de origem para efetivação administrativa."
            )

        ciclo_destino = refinanciamento.ciclo_destino or get_next_cycle(contrato, ciclo_origem)
        if ciclo_destino is None:
            ciclo_destino = (
                Ciclo.objects.filter(
                    contrato=contrato,
                    numero=ciclo_origem.numero + 1,
                    deleted_at__isnull=True,
                )
                .order_by("id")
                .first()
            )
        if ciclo_destino is None:
            raise ValidationError(
                "Não foi encontrado o ciclo seguinte para materializar esta renovação."
            )

        activation_payload = get_cycle_activation_payload(ciclo_destino)
        activation_at = activation_payload.get("data_ativacao_ciclo")
        if activation_at is None:
            activation_at = ciclo_destino.created_at or timezone.now()

        refinanciamento.status = Refinanciamento.Status.EFETIVADO
        refinanciamento.ciclo_destino = ciclo_destino
        refinanciamento.executado_em = activation_at
        refinanciamento.data_ativacao_ciclo = activation_at
        refinanciamento.efetivado_por = user
        refinanciamento.reviewed_by = refinanciamento.reviewed_by or user
        refinanciamento.reviewed_at = refinanciamento.reviewed_at or activation_at
        refinanciamento.aprovado_por = refinanciamento.aprovado_por or user
        refinanciamento.observacao = motivo
        refinanciamento.motivo_bloqueio = ""
        refinanciamento.save(
            update_fields=[
                "status",
                "ciclo_destino",
                "executado_em",
                "data_ativacao_ciclo",
                "efetivado_por",
                "reviewed_by",
                "reviewed_at",
                "aprovado_por",
                "observacao",
                "motivo_bloqueio",
                "updated_at",
            ]
        )
        AdminOverrideService._finalize_refinanciamento_assumption(
            refinanciamento=refinanciamento,
            finalized=True,
            user=user,
        )
        AdminOverrideService._transition_renewal_esteira(
            refinanciamento=refinanciamento,
            target_status=Refinanciamento.Status.EFETIVADO,
            motivo=motivo,
            user=user,
        )
        return refinanciamento

    @staticmethod
    def _revert_refinanciamento_keep_associado_active(
        *,
        refinanciamento: Refinanciamento,
        motivo: str,
        user,
    ) -> Refinanciamento:
        refinanciamento.status = Refinanciamento.Status.REVERTIDO
        refinanciamento.executado_em = None
        refinanciamento.data_ativacao_ciclo = None
        refinanciamento.ciclo_destino = None
        refinanciamento.efetivado_por = None
        refinanciamento.observacao = motivo
        refinanciamento.motivo_bloqueio = ""
        refinanciamento.save(
            update_fields=[
                "status",
                "executado_em",
                "data_ativacao_ciclo",
                "ciclo_destino",
                "efetivado_por",
                "observacao",
                "motivo_bloqueio",
                "updated_at",
            ]
        )
        AdminOverrideService._finalize_refinanciamento_assumption(
            refinanciamento=refinanciamento,
            finalized=True,
            user=user,
        )
        AdminOverrideService._transition_renewal_esteira(
            refinanciamento=refinanciamento,
            target_status=Refinanciamento.Status.REVERTIDO,
            motivo=motivo,
            user=user,
        )
        return refinanciamento

    @staticmethod
    def _projection_requires_renewal_queue_clear(
        *,
        contrato: Contrato,
        projection: dict[str, Any],
    ) -> bool:
        status_renovacao = str(projection.get("status_renovacao") or "")
        if status_renovacao in APT_LIKE_OPERATIONAL_REFINANCIAMENTO_STATUSES:
            return True

        cycles = list(sorted(projection.get("cycles") or [], key=lambda item: item["numero"]))
        if not cycles:
            return False
        latest_cycle = cycles[-1]
        if (
            str(latest_cycle.get("status") or "") == Ciclo.Status.APTO_A_RENOVAR
            or str(latest_cycle.get("fase_ciclo") or "") == "apto_a_renovar"
        ):
            return True
        return is_contract_eligible_for_renewal_competencia(
            contrato,
            projection=projection,
        )

    @staticmethod
    def _clear_renewal_queue_for_active_associado(
        *,
        associado: Associado,
        motivo: str,
        user,
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for contrato in get_operational_contracts_for_associado(associado):
            contrato.refresh_from_db()
            if contrato.status not in {Contrato.Status.ATIVO, Contrato.Status.ENCERRADO}:
                continue

            projection = build_contract_cycle_projection(contrato)
            refinanciamento = _active_operational_refinanciamento(contrato)
            if refinanciamento is None:
                if not AdminOverrideService._projection_requires_renewal_queue_clear(
                    contrato=contrato,
                    projection=projection,
                ):
                    continue
                refinanciamento = AdminOverrideService._materialize_safe_transition_refinanciamento(
                    contrato=contrato,
                    projection=projection,
                    user=user,
                )

            if (
                refinanciamento.executado_em is not None
                or refinanciamento.data_ativacao_ciclo is not None
                or refinanciamento.ciclo_destino_id is not None
            ):
                continue

            before = _serialize_refinanciamento(refinanciamento)
            refinanciamento = AdminOverrideService._revert_refinanciamento_keep_associado_active(
                refinanciamento=refinanciamento,
                motivo=motivo,
                user=user,
            )
            after = _serialize_refinanciamento(refinanciamento)
            changes.append(
                {
                    "entity_type": AdminOverrideChange.EntityType.REFINANCIAMENTO,
                    "entity_id": refinanciamento.id,
                    "resumo": "Renovação operacional removida ao manter associado ativo",
                    "before_snapshot": before,
                    "after_snapshot": after,
                }
            )

        return changes

    @staticmethod
    def _materializable_refinanciamento_for_effectivation(
        contrato: Contrato,
    ) -> Refinanciamento | None:
        candidates = (
            Refinanciamento.objects.filter(
                contrato_origem=contrato,
                deleted_at__isnull=True,
                legacy_refinanciamento_id__isnull=True,
                origem=Refinanciamento.Origem.OPERACIONAL,
                ciclo_destino__isnull=True,
                executado_em__isnull=True,
                data_ativacao_ciclo__isnull=True,
            )
            .filter(
                status__in=[
                    Refinanciamento.Status.APTO_A_RENOVAR,
                    Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO,
                    Refinanciamento.Status.EM_ANALISE_RENOVACAO,
                    Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
                    Refinanciamento.Status.PENDENTE_TERMO_AGENTE,
                    Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
                    Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
                    Refinanciamento.Status.PENDENTE_APTO,
                    Refinanciamento.Status.SOLICITADO,
                    Refinanciamento.Status.EM_ANALISE,
                    Refinanciamento.Status.APROVADO,
                    Refinanciamento.Status.CONCLUIDO,
                ]
            )
            .order_by("-competencia_solicitada", "-created_at", "-id")
        )
        for refinanciamento in candidates:
            if not refinanciamento.ciclo_origem_id:
                continue
            has_next_cycle = Ciclo.objects.filter(
                contrato=contrato,
                numero=refinanciamento.ciclo_origem.numero + 1,
                deleted_at__isnull=True,
            ).exists()
            if has_next_cycle:
                return refinanciamento
        return None

    @staticmethod
    def _force_refinanciamento_status(
        *,
        refinanciamento: Refinanciamento,
        target_status: str,
        motivo: str,
        user,
    ) -> Refinanciamento:
        normalized = target_status
        if (
            refinanciamento.executado_em is not None
            or refinanciamento.data_ativacao_ciclo is not None
            or refinanciamento.ciclo_destino_id is not None
        ):
            raise ValidationError(
                "Esta renovação já foi materializada. Não é seguro reposicioná-la pelo editor."
            )

        before = _serialize_refinanciamento(refinanciamento)
        if normalized == Refinanciamento.Status.EFETIVADO:
            refinanciamento = AdminOverrideService._effectivate_refinanciamento_from_existing_cycles(
                refinanciamento=refinanciamento,
                motivo=motivo,
                user=user,
            )
        elif normalized == Refinanciamento.Status.REVERTIDO:
            refinanciamento = AdminOverrideService._revert_refinanciamento_keep_associado_active(
                refinanciamento=refinanciamento,
                motivo=motivo,
                user=user,
            )
        else:
            refinanciamento.status = normalized
            refinanciamento.observacao = motivo
            if normalized == Refinanciamento.Status.APTO_A_RENOVAR:
                refinanciamento.reviewed_by = None
                refinanciamento.reviewed_at = None
                refinanciamento.aprovado_por = None
            elif normalized in {
                Refinanciamento.Status.EM_ANALISE_RENOVACAO,
                Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
                Refinanciamento.Status.PENDENTE_TERMO_AGENTE,
            }:
                refinanciamento.reviewed_by = None
                refinanciamento.reviewed_at = None
                refinanciamento.aprovado_por = None
            elif normalized in {
                Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
                Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
            }:
                refinanciamento.reviewed_by = user
                refinanciamento.reviewed_at = timezone.now()
                if normalized == Refinanciamento.Status.APROVADO_PARA_RENOVACAO:
                    refinanciamento.aprovado_por = user

            refinanciamento.save()

        assumption = None
        if refinanciamento.cycle_key:
            assumption = (
                Assumption.objects.filter(
                    cadastro=refinanciamento.associado,
                    request_key=refinanciamento.cycle_key,
                )
                .order_by("-created_at", "-id")
                .first()
            )
        if assumption is not None:
            if normalized in {
                Refinanciamento.Status.APTO_A_RENOVAR,
                Refinanciamento.Status.EM_ANALISE_RENOVACAO,
                Refinanciamento.Status.PENDENTE_TERMO_ANALISTA,
            }:
                assumption.status = Assumption.Status.LIBERADO
                assumption.liberado_em = timezone.now()
                assumption.assumido_em = None
                assumption.finalizado_em = None
                if normalized == Refinanciamento.Status.APTO_A_RENOVAR:
                    assumption.analista = None
                assumption.heartbeat_at = None
                assumption.save(
                    update_fields=[
                        "status",
                        "liberado_em",
                        "assumido_em",
                        "finalizado_em",
                        "analista",
                        "heartbeat_at",
                        "updated_at",
                    ]
                )
            elif normalized in {
                Refinanciamento.Status.PENDENTE_TERMO_AGENTE,
                Refinanciamento.Status.APROVADO_ANALISE_RENOVACAO,
                Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
                Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO,
            }:
                assumption.status = Assumption.Status.FINALIZADO
                if assumption.analista_id is None:
                    assumption.analista = user
                assumption.finalizado_em = timezone.now()
                assumption.assumido_em = None
                assumption.heartbeat_at = None
                assumption.save(
                    update_fields=[
                        "status",
                        "analista",
                        "finalizado_em",
                        "assumido_em",
                        "heartbeat_at",
                        "updated_at",
                    ]
                )

        contrato = refinanciamento.contrato_origem
        if contrato is not None and normalized in {
            Refinanciamento.Status.APROVADO_PARA_RENOVACAO,
            Refinanciamento.Status.SOLICITADO_PARA_LIQUIDACAO,
        }:
            from apps.refinanciamento.services import RefinanciamentoService

            RefinanciamentoService._mover_esteira_para_tesouraria(
                contrato,
                user,
                acao="admin_safe_renewal_stage_transition",
                observacao=motivo,
            )
        else:
            AdminOverrideService._transition_renewal_esteira(
                refinanciamento=refinanciamento,
                target_status=normalized,
                motivo=motivo,
                user=user,
            )

        after = _serialize_refinanciamento(refinanciamento)
        AdminOverrideService._record_event(
            context=_OverrideContext(
                associado=refinanciamento.associado,
                contrato=refinanciamento.contrato_origem,
                refinanciamento=refinanciamento,
            ),
            user=user,
            escopo=AdminOverrideEvent.Scope.REFINANCIAMENTO,
            resumo=f"Transição administrativa segura para {SAFE_RENEWAL_STAGE_LABELS.get(normalized, normalized)}",
            motivo=motivo,
            before_snapshot=before,
            after_snapshot=after,
            changes=[
                {
                    "entity_type": AdminOverrideChange.EntityType.REFINANCIAMENTO,
                    "entity_id": refinanciamento.id,
                    "resumo": f"Renovação reposicionada para {SAFE_RENEWAL_STAGE_LABELS.get(normalized, normalized)}",
                    "before_snapshot": before,
                    "after_snapshot": after,
                }
            ],
        )
        return refinanciamento

    @staticmethod
    def apply_safe_renewal_stage_transition(
        *,
        associado: Associado,
        payload: dict[str, Any],
        user,
    ) -> dict[str, Any]:
        motivo = str(payload.get("motivo") or "").strip()
        if not motivo:
            raise ValidationError("O motivo da alteração é obrigatório.")

        contrato = AdminOverrideService._resolve_contract_for_safe_transition(
            associado=associado,
            contrato_id=int(payload.get("contrato_id") or 0),
        )
        target_stage = str(payload.get("target_stage") or "").strip()
        if target_stage not in SAFE_RENEWAL_STAGE_LABELS:
            raise ValidationError("Etapa de renovação inválida para transição administrativa.")

        if target_stage != Refinanciamento.Status.EFETIVADO:
            rebuild_contract_cycle_state(
                contrato,
                execute=True,
                force_active_operational_status=Refinanciamento.Status.PENDENTE_APTO,
            )
        contrato.refresh_from_db()
        projection = AdminOverrideService.build_contract_projection_for_response(
            contrato,
            include_documents=False,
        )
        refinanciamento = (
            AdminOverrideService._materializable_refinanciamento_for_effectivation(
                contrato,
            )
            if target_stage == Refinanciamento.Status.EFETIVADO
            else _active_operational_refinanciamento(contrato)
        )
        if refinanciamento is None:
            refinanciamento = _active_operational_refinanciamento(contrato)
        if refinanciamento is None:
            refinanciamento = AdminOverrideService._materialize_safe_transition_refinanciamento(
                contrato=contrato,
                projection=projection,
                user=user,
            )

        AdminOverrideService._force_refinanciamento_status(
            refinanciamento=refinanciamento,
            target_status=target_stage,
            motivo=motivo,
            user=user,
        )

        rebuild_kwargs: dict[str, Any] = {"execute": True}
        if target_stage not in {
            Refinanciamento.Status.EFETIVADO,
            Refinanciamento.Status.REVERTIDO,
        }:
            rebuild_kwargs["force_active_operational_status"] = target_stage
        rebuild_contract_cycle_state(
            contrato,
            **rebuild_kwargs,
        )
        sync_associado_mother_status(associado)
        associado.refresh_from_db()
        return AdminOverrideService.build_associado_editor_payload(associado)

    @staticmethod
    def build_contract_projection_for_response(contrato: Contrato, *, include_documents: bool = True) -> dict[str, Any]:
        return build_contract_cycle_projection(contrato, include_documents=include_documents)

    @staticmethod
    def _record_event(
        *,
        context: _OverrideContext,
        user,
        escopo: str,
        resumo: str,
        motivo: str,
        before_snapshot: dict[str, Any],
        after_snapshot: dict[str, Any],
        changes: list[dict[str, Any]],
    ) -> AdminOverrideEvent:
        event = AdminOverrideEvent.objects.create(
            associado=context.associado,
            contrato=context.contrato,
            ciclo=context.ciclo,
            parcela=context.parcela,
            refinanciamento=context.refinanciamento,
            documento=context.documento,
            comprovante=context.comprovante,
            realizado_por=user,
            escopo=escopo,
            resumo=resumo,
            motivo=motivo,
            before_snapshot=_json_safe(before_snapshot),
            after_snapshot=_json_safe(after_snapshot),
            confirmacao_dupla=True,
        )
        for change in changes:
            AdminOverrideChange.objects.create(
                evento=event,
                entity_type=change["entity_type"],
                entity_id=change["entity_id"],
                competencia_referencia=change.get("competencia_referencia"),
                resumo=change["resumo"],
                before_snapshot=_json_safe(change.get("before_snapshot") or {}),
                after_snapshot=_json_safe(change.get("after_snapshot") or {}),
            )
        return event

    @staticmethod
    def apply_associado_core_override(
        *,
        associado: Associado,
        payload: dict[str, Any],
        user,
    ) -> Associado:
        with transaction.atomic():
            _assert_version(associado, payload.get("updated_at"))
            contrato = resolve_operational_contract_for_associado(associado)
            if contrato is not None:
                _assert_version(contrato, payload.get("contrato_updated_at"))
            motivo = str(payload.get("motivo") or "").strip()
            if not motivo:
                raise ValidationError("O motivo da alteração é obrigatório.")

            before_associado = _serialize_associado(associado)
            before_contrato = (
                _serialize_contrato(contrato, include_ciclos=False) if contrato else {}
            )
            requested_status = str(payload.get("status") or "").strip()

            simple_fields = [
                "nome_completo",
                "rg",
                "orgao_expedidor",
                "profissao",
                "estado_civil",
                "cargo",
                "status",
                "observacao",
            ]
            for field in simple_fields:
                if field in payload:
                    setattr(associado, field, payload.get(field) or "")
            if "data_nascimento" in payload:
                associado.data_nascimento = _parse_optional_date(
                    payload.get("data_nascimento")
                )
            if "agente_responsavel_id" in payload:
                associado.agente_responsavel_id = payload.get("agente_responsavel_id")
            if (
                "percentual_repasse" in payload
                and payload.get("percentual_repasse") not in (None, "")
            ):
                associado.auxilio_taxa = _parse_decimal(payload.get("percentual_repasse"))

            endereco = payload.get("endereco") or {}
            if endereco:
                associado.cep = str(endereco.get("cep") or associado.cep)
                associado.logradouro = str(
                    endereco.get("logradouro")
                    or endereco.get("endereco")
                    or associado.logradouro
                )
                associado.numero = str(endereco.get("numero") or associado.numero)
                associado.complemento = str(
                    endereco.get("complemento") or associado.complemento
                )
                associado.bairro = str(endereco.get("bairro") or associado.bairro)
                associado.cidade = str(endereco.get("cidade") or associado.cidade)
                associado.uf = str(endereco.get("uf") or associado.uf)

            dados_bancarios = payload.get("dados_bancarios") or {}
            if dados_bancarios:
                associado.banco = str(dados_bancarios.get("banco") or associado.banco)
                associado.agencia = str(
                    dados_bancarios.get("agencia") or associado.agencia
                )
                associado.conta = str(dados_bancarios.get("conta") or associado.conta)
                associado.tipo_conta = str(
                    dados_bancarios.get("tipo_conta") or associado.tipo_conta
                )
                associado.chave_pix = str(
                    dados_bancarios.get("chave_pix") or associado.chave_pix
                )

            contato = payload.get("contato") or {}
            if contato:
                associado.telefone = str(contato.get("celular") or associado.telefone)
                associado.email = str(contato.get("email") or associado.email)
                associado.orgao_publico = str(
                    contato.get("orgao_publico") or associado.orgao_publico
                )
                associado.situacao_servidor = str(
                    contato.get("situacao_servidor") or associado.situacao_servidor
                )
                associado.matricula_orgao = str(
                    contato.get("matricula_servidor") or associado.matricula_orgao
                )

            associado.save()
            renewal_queue_changes: list[dict[str, Any]] = []
            if requested_status == Associado.Status.ATIVO:
                renewal_queue_changes = (
                    AdminOverrideService._clear_renewal_queue_for_active_associado(
                        associado=associado,
                        motivo=motivo,
                        user=user,
                    )
                )
                if associado.status != Associado.Status.ATIVO:
                    associado.status = Associado.Status.ATIVO
                    associado.save(update_fields=["status", "updated_at"])
                associado.refresh_from_db()

            if contrato is not None:
                contract_fields = {
                    "valor_bruto": "valor_bruto_total",
                    "valor_liquido": "valor_liquido",
                    "prazo_meses": "prazo_meses",
                    "taxa_antecipacao": "taxa_antecipacao",
                    "valor_mensalidade": "mensalidade",
                    "margem_disponivel": "margem_disponivel",
                }
                prazo_changed_save_all = False
                old_prazo_save_all = int(contrato.prazo_meses or 0)
                comissao_no_payload = (
                    "comissao_agente" in payload
                    and payload.get("comissao_agente") not in (None, "")
                )
                for field, source_key in contract_fields.items():
                    if source_key not in payload or payload.get(source_key) in (None, ""):
                        continue
                    value = payload.get(source_key)
                    if field == "valor_mensalidade":
                        value = _assert_override_mensalidade_allowed(
                            current_value=contrato.valor_mensalidade,
                            next_value=value,
                            field_name="mensalidade",
                        )
                    if field == "prazo_meses":
                        new_prazo = int(value)
                        if new_prazo not in (3, 4):
                            raise ValidationError(
                                "prazo_meses deve ser 3 ou 4 (tamanho do ciclo de parcelas)."
                            )
                        if old_prazo_save_all != new_prazo:
                            prazo_changed_save_all = True
                            # Recalcula comissao proporcionalmente, exceto se o
                            # operador estiver sobrescrevendo explicitamente.
                            if (
                                not comissao_no_payload
                                and old_prazo_save_all > 0
                                and contrato.comissao_agente
                            ):
                                contrato.comissao_agente = (
                                    (
                                        Decimal(contrato.comissao_agente)
                                        * Decimal(new_prazo)
                                    )
                                    / Decimal(old_prazo_save_all)
                                ).quantize(Decimal("0.01"))
                        setattr(contrato, field, new_prazo)
                    else:
                        setattr(contrato, field, _parse_decimal(value))
                if "status_contrato" in payload:
                    contrato.status = str(payload.get("status_contrato") or contrato.status)
                if "agente_responsavel_id" in payload:
                    contrato.agente_id = payload.get("agente_responsavel_id")
                contrato.save()
                if prazo_changed_save_all:
                    rebuild_contract_cycle_state(contrato)
                    contrato.refresh_from_db()

            after_associado = _serialize_associado(associado)
            after_contrato = (
                _serialize_contrato(contrato, include_ciclos=False) if contrato else {}
            )
            AdminOverrideService._record_event(
                context=_OverrideContext(associado=associado, contrato=contrato),
                user=user,
                escopo=AdminOverrideEvent.Scope.ASSOCIADO,
                resumo="Atualização administrativa do cadastro do associado",
                motivo=motivo,
                before_snapshot={
                    "associado": before_associado,
                    "contrato": before_contrato,
                },
                after_snapshot={
                    "associado": after_associado,
                    "contrato": after_contrato,
                },
                changes=[
                    {
                        "entity_type": AdminOverrideChange.EntityType.ASSOCIADO,
                        "entity_id": associado.id,
                        "resumo": "Dados do associado atualizados",
                        "before_snapshot": before_associado,
                        "after_snapshot": after_associado,
                    },
                    *(
                        [
                            {
                                "entity_type": AdminOverrideChange.EntityType.CONTRATO,
                                "entity_id": contrato.id,
                                "resumo": "Dados do contrato atualizados via cadastro do associado",
                                "before_snapshot": before_contrato,
                                "after_snapshot": after_contrato,
                            }
                        ]
                        if contrato is not None
                        else []
                    ),
                    *renewal_queue_changes,
                ],
            )
            return associado

    @staticmethod
    def apply_contract_core_override(
        *,
        contrato: Contrato,
        payload: dict[str, Any],
        user,
        wrap_atomic: bool = True,
    ) -> Contrato:
        atomic_ctx = transaction.atomic() if wrap_atomic else nullcontext()
        with atomic_ctx:
            _assert_version(contrato, payload.get("updated_at"))
            motivo = str(payload.get("motivo") or "").strip()
            if not motivo:
                raise ValidationError("O motivo da alteração é obrigatório.")
            before = _serialize_contrato(contrato, include_ciclos=False)
            numeric_fields = {
                "valor_bruto",
                "valor_liquido",
                "valor_mensalidade",
                "taxa_antecipacao",
                "margem_disponivel",
                "valor_total_antecipacao",
                "doacao_associado",
                "comissao_agente",
            }
            for field in [
                "status",
                "contato_web",
                "termos_web",
                "agente_id",
            ]:
                if field in payload:
                    setattr(contrato, field, payload.get(field))
            prazo_changed = False
            old_prazo_meses = int(contrato.prazo_meses or 0)
            if "prazo_meses" in payload and payload.get("prazo_meses") not in (None, ""):
                new_prazo = int(payload.get("prazo_meses"))
                if new_prazo not in (3, 4):
                    raise ValidationError(
                        "prazo_meses deve ser 3 ou 4 (tamanho do ciclo de parcelas)."
                    )
                if old_prazo_meses != new_prazo:
                    contrato.prazo_meses = new_prazo
                    # Recalcula a comissao do agente proporcionalmente ao novo
                    # tamanho do ciclo. Apenas se "comissao_agente" nao vier
                    # explicitamente no payload (operador pode sobrescrever).
                    payload_has_comissao = (
                        "comissao_agente" in payload
                        and payload.get("comissao_agente") not in (None, "")
                    )
                    if (
                        not payload_has_comissao
                        and old_prazo_meses > 0
                        and contrato.comissao_agente
                    ):
                        contrato.comissao_agente = (
                            (Decimal(contrato.comissao_agente) * Decimal(new_prazo))
                            / Decimal(old_prazo_meses)
                        ).quantize(Decimal("0.01"))
                    prazo_changed = True
            for field in numeric_fields:
                if field in payload and payload.get(field) not in (None, ""):
                    value = payload.get(field)
                    if field == "valor_mensalidade":
                        value = _assert_override_mensalidade_allowed(
                            current_value=contrato.valor_mensalidade,
                            next_value=value,
                            field_name="valor_mensalidade",
                        )
                    else:
                        value = _parse_decimal(value)
                    setattr(contrato, field, value)
            for field in [
                "data_contrato",
                "data_aprovacao",
                "data_primeira_mensalidade",
                "mes_averbacao",
                "auxilio_liberado_em",
            ]:
                if field in payload:
                    setattr(contrato, field, _parse_optional_date(payload.get(field)))
            contrato.save()
            if prazo_changed:
                rebuild_contract_cycle_state(contrato)
                contrato.refresh_from_db()
            after = _serialize_contrato(contrato, include_ciclos=False)
            AdminOverrideService._record_event(
                context=_OverrideContext(associado=contrato.associado, contrato=contrato),
                user=user,
                escopo=AdminOverrideEvent.Scope.CONTRATO,
                resumo=f"Atualização administrativa do contrato {contrato.codigo}",
                motivo=motivo,
                before_snapshot=before,
                after_snapshot=after,
                changes=[
                    {
                        "entity_type": AdminOverrideChange.EntityType.CONTRATO,
                        "entity_id": contrato.id,
                        "resumo": "Dados do contrato atualizados",
                        "before_snapshot": before,
                        "after_snapshot": after,
                    }
                ],
            )
            return contrato

    @staticmethod
    def apply_refinanciamento_override(
        *,
        refinanciamento: Refinanciamento,
        payload: dict[str, Any],
        user,
        wrap_atomic: bool = True,
        sync_associado_status: bool = True,
    ) -> Refinanciamento:
        atomic_ctx = transaction.atomic() if wrap_atomic else nullcontext()
        with atomic_ctx:
            _assert_version(refinanciamento, payload.get("updated_at"))
            motivo = str(payload.get("motivo") or "").strip()
            if not motivo:
                raise ValidationError("O motivo da alteração é obrigatório.")
            requested_status = str(payload.get("status") or "").strip()
            if requested_status and requested_status != refinanciamento.status:
                raise ValidationError(ADMIN_REFINANCIAMENTO_EDITOR_STATUS_ERROR)
            before = _serialize_refinanciamento(refinanciamento)
            for field in [
                "motivo_bloqueio",
                "observacao",
                "analista_note",
                "coordenador_note",
            ]:
                if field in payload:
                    setattr(refinanciamento, field, payload.get(field) or "")
            for field in ["competencia_solicitada"]:
                if field in payload:
                    setattr(
                        refinanciamento,
                        field,
                        _parse_optional_date(payload.get(field)),
                    )
            for field in ["valor_refinanciamento", "repasse_agente"]:
                if field in payload and payload.get(field) not in (None, ""):
                    setattr(refinanciamento, field, _parse_decimal(payload.get(field)))
            for field in ["executado_em", "data_ativacao_ciclo"]:
                if field in payload:
                    raw = payload.get(field)
                    setattr(refinanciamento, field, parse_datetime(str(raw)) if raw else None)
            if "reviewed_by_id" in payload:
                refinanciamento.reviewed_by_id = payload.get("reviewed_by_id")
            refinanciamento.save()
            if sync_associado_status:
                sync_associado_mother_status(refinanciamento.associado)
            after = _serialize_refinanciamento(refinanciamento)
            AdminOverrideService._record_event(
                context=_OverrideContext(
                    associado=refinanciamento.associado,
                    contrato=refinanciamento.contrato_origem,
                    refinanciamento=refinanciamento,
                ),
                user=user,
                escopo=AdminOverrideEvent.Scope.REFINANCIAMENTO,
                resumo="Atualização administrativa da renovação",
                motivo=motivo,
                before_snapshot=before,
                after_snapshot=after,
                changes=[
                    {
                        "entity_type": AdminOverrideChange.EntityType.REFINANCIAMENTO,
                        "entity_id": refinanciamento.id,
                        "resumo": "Dados da renovação atualizados",
                        "before_snapshot": before,
                        "after_snapshot": after,
                    }
                ],
            )
            return refinanciamento

    @staticmethod
    def apply_esteira_override(
        *,
        associado: Associado,
        payload: dict[str, Any],
        user,
        wrap_atomic: bool = True,
    ) -> EsteiraItem:
        atomic_ctx = transaction.atomic() if wrap_atomic else nullcontext()
        with atomic_ctx:
            esteira = getattr(associado, "esteira_item", None)
            if esteira is None:
                raise ValidationError("Associado sem item de esteira.")
            _assert_version(esteira, payload.get("updated_at"))
            motivo = str(payload.get("motivo") or "").strip()
            if not motivo:
                raise ValidationError("O motivo da alteração é obrigatório.")
            before = _serialize_esteira(esteira)
            previous_stage = esteira.etapa_atual
            previous_status = esteira.status
            if "etapa_atual" in payload:
                esteira.etapa_atual = str(
                    payload.get("etapa_atual") or esteira.etapa_atual
                )
            if "status" in payload:
                esteira.status = str(payload.get("status") or esteira.status)
            if "prioridade" in payload and payload.get("prioridade") not in (None, ""):
                esteira.prioridade = int(payload.get("prioridade"))
            if "observacao" in payload:
                esteira.observacao = str(payload.get("observacao") or "")
            esteira.save()
            _normalize_esteira_conclusion_state(esteira, explicit_transition=True)
            Transicao.objects.create(
                esteira_item=esteira,
                acao="admin_override",
                de_status=previous_stage,
                para_status=esteira.etapa_atual,
                de_situacao=previous_status,
                para_situacao=esteira.status,
                realizado_por=user,
                observacao=motivo,
            )
            after = _serialize_esteira(esteira)
            AdminOverrideService._record_event(
                context=_OverrideContext(associado=associado),
                user=user,
                escopo=AdminOverrideEvent.Scope.ESTEIRA,
                resumo="Override administrativo da esteira",
                motivo=motivo,
                before_snapshot=before,
                after_snapshot=after,
                changes=[
                    {
                        "entity_type": AdminOverrideChange.EntityType.ESTEIRA,
                        "entity_id": esteira.id,
                        "resumo": "Status da esteira atualizado",
                        "before_snapshot": before,
                        "after_snapshot": after,
                    }
                ],
            )
            return esteira

    @staticmethod
    def _create_document_version(
        *,
        documento: Documento,
        payload: dict[str, Any],
        user,
    ) -> Documento:
        new_documento = Documento.objects.create(
            associado=documento.associado,
            tipo=documento.tipo,
            arquivo=payload.get("arquivo") or documento.arquivo,
            origem=documento.origem,
            status=payload.get("status") or documento.status,
            observacao=payload.get("observacao") or documento.observacao,
        )
        return new_documento

    @staticmethod
    def versionar_documento(
        *,
        documento: Documento,
        payload: dict[str, Any],
        user,
        request=None,
    ) -> Documento:
        _assert_version(documento, payload.get("updated_at"))
        motivo = str(payload.get("motivo") or "").strip()
        if not motivo:
            raise ValidationError("O motivo da alteração é obrigatório.")
        before = _serialize_documento(documento, request=request)
        with transaction.atomic():
            novo = AdminOverrideService._create_document_version(
                documento=documento,
                payload=payload,
                user=user,
            )
            after = _serialize_documento(novo, request=request)
            AdminOverrideService._record_event(
                context=_OverrideContext(
                    associado=documento.associado,
                    documento=novo,
                ),
                user=user,
                escopo=AdminOverrideEvent.Scope.DOCUMENTO,
                resumo="Nova versão de documento do associado",
                motivo=motivo,
                before_snapshot=before,
                after_snapshot=after,
                changes=[
                    {
                        "entity_type": AdminOverrideChange.EntityType.DOCUMENTO,
                        "entity_id": novo.id,
                        "resumo": "Documento versionado pelo admin",
                        "before_snapshot": before,
                        "after_snapshot": after,
                    }
                ],
            )
            return novo

    @staticmethod
    def versionar_comprovante(
        *,
        comprovante: Comprovante,
        payload: dict[str, Any],
        user,
        request=None,
    ) -> Comprovante:
        _assert_version(comprovante, payload.get("updated_at"))
        motivo = str(payload.get("motivo") or "").strip()
        if not motivo:
            raise ValidationError("O motivo da alteração é obrigatório.")
        before = _serialize_comprovante(comprovante, request=request)
        with transaction.atomic():
            novo = Comprovante.objects.create(
                refinanciamento=comprovante.refinanciamento,
                contrato=comprovante.contrato,
                ciclo=comprovante.ciclo,
                tipo=payload.get("tipo") or comprovante.tipo,
                papel=payload.get("papel") or comprovante.papel,
                arquivo=payload.get("arquivo") or comprovante.arquivo,
                mime=getattr(payload.get("arquivo"), "content_type", "") or comprovante.mime,
                size_bytes=getattr(payload.get("arquivo"), "size", None) or comprovante.size_bytes,
                data_pagamento=(
                    parse_datetime(str(payload.get("data_pagamento")))
                    if payload.get("data_pagamento")
                    else comprovante.data_pagamento
                ),
                origem=payload.get("origem") or comprovante.origem,
                agente_snapshot=comprovante.agente_snapshot,
                filial_snapshot=comprovante.filial_snapshot,
                enviado_por=user,
                status_validacao=payload.get("status_validacao") or comprovante.status_validacao,
            )
            after = _serialize_comprovante(novo, request=request)
            AdminOverrideService._record_event(
                context=_OverrideContext(
                    associado=novo.contrato.associado if novo.contrato_id else novo.refinanciamento.associado,
                    contrato=novo.contrato or novo.refinanciamento.contrato_origem,
                    comprovante=novo,
                    refinanciamento=novo.refinanciamento,
                ),
                user=user,
                escopo=AdminOverrideEvent.Scope.COMPROVANTE,
                resumo="Nova versão de comprovante",
                motivo=motivo,
                before_snapshot=before,
                after_snapshot=after,
                changes=[
                    {
                        "entity_type": AdminOverrideChange.EntityType.COMPROVANTE,
                        "entity_id": novo.id,
                        "resumo": "Comprovante versionado pelo admin",
                        "before_snapshot": before,
                        "after_snapshot": after,
                    }
                ],
            )
            return novo

    @staticmethod
    def criar_comprovantes_ciclo(
        *,
        ciclo: Ciclo,
        payload: dict[str, Any],
        user,
        request=None,
    ) -> list[Comprovante]:
        motivo = str(payload.get("motivo") or "").strip()
        arquivos = [arquivo for arquivo in payload.get("arquivos", []) if arquivo]
        if not motivo:
            raise ValidationError("O motivo da alteração é obrigatório.")
        if not arquivos:
            raise ValidationError("Envie ao menos um comprovante para o ciclo.")

        before = {
            "ciclo_id": ciclo.id,
            "contrato_id": ciclo.contrato_id,
            "comprovantes_ids": list(
                Comprovante.objects.filter(ciclo=ciclo, deleted_at__isnull=True).values_list("id", flat=True)
            ),
        }

        with transaction.atomic():
            criados: list[Comprovante] = []
            for arquivo in arquivos:
                criado = Comprovante.objects.create(
                    contrato=ciclo.contrato,
                    ciclo=ciclo,
                    tipo=payload.get("tipo") or Comprovante.Tipo.OUTRO,
                    papel=payload.get("papel") or Comprovante.Papel.OPERACIONAL,
                    arquivo=arquivo,
                    mime=getattr(arquivo, "content_type", "") or "",
                    size_bytes=getattr(arquivo, "size", None),
                    data_pagamento=(
                        parse_datetime(str(payload.get("data_pagamento")))
                        if payload.get("data_pagamento")
                        else None
                    ),
                    origem=payload.get("origem") or Comprovante.Origem.OUTRO,
                    agente_snapshot=getattr(ciclo.contrato.agente, "full_name", "") or "",
                    filial_snapshot="",
                    enviado_por=user,
                    status_validacao=(
                        payload.get("status_validacao")
                        or Comprovante.StatusValidacao.PENDENTE
                    ),
                )
                criados.append(criado)

            after = [_serialize_comprovante(item, request=request) for item in criados]
            AdminOverrideService._record_event(
                context=_OverrideContext(
                    associado=ciclo.contrato.associado,
                    contrato=ciclo.contrato,
                    ciclo=ciclo,
                    comprovante=criados[-1],
                ),
                user=user,
                escopo=AdminOverrideEvent.Scope.COMPROVANTE,
                resumo=(
                    f"Inclusão de {len(criados)} comprovante(s) no ciclo {ciclo.numero}"
                ),
                motivo=motivo,
                before_snapshot=before,
                after_snapshot={"created": after},
                changes=[
                    {
                        "entity_type": AdminOverrideChange.EntityType.COMPROVANTE,
                        "entity_id": item.id,
                        "resumo": "Comprovante incluído pelo admin no ciclo",
                        "before_snapshot": {},
                        "after_snapshot": serialized,
                    }
                    for item, serialized in zip(criados, after, strict=False)
                ],
            )
            return criados

    @staticmethod
    def apply_cycle_layout_override(
        *,
        contrato: Contrato,
        payload: dict[str, Any],
        user,
        wrap_atomic: bool = True,
        sync_associado_status: bool = True,
    ) -> tuple[Contrato, list[dict[str, Any]]]:
        _assert_version(contrato, payload.get("updated_at"))
        motivo = str(payload.get("motivo") or "").strip()
        if not motivo:
            raise ValidationError("O motivo da alteração é obrigatório.")
        operation_warnings: list[dict[str, Any]] = []

        before_snapshot = AdminOverrideService.build_contract_projection_for_response(
            contrato,
            include_documents=True,
        )

        all_cycles = list(Ciclo.all_objects.filter(contrato=contrato))
        current_cycles = {
            ciclo.id: ciclo
            for ciclo in all_cycles
            if ciclo.deleted_at is None
        }
        all_parcelas = list(Parcela.all_objects.filter(ciclo__contrato=contrato))
        current_parcelas = {
            parcela.id: parcela
            for parcela in all_parcelas
            if parcela.deleted_at is None and parcela.status != Parcela.Status.CANCELADO
        }
        old_reference_by_parcela_id = {
            parcela.id: parcela.referencia_mes
            for parcela in current_parcelas.values()
        }

        cycles_payload = [dict(item) for item in (payload.get("cycles") or [])]
        parcel_items = [dict(item) for item in (payload.get("parcelas") or [])]
        if not cycles_payload and not parcel_items:
            raise ValidationError("Informe ao menos um ciclo no layout.")

        if not cycles_payload:
            if current_cycles:
                cycles_payload = [
                    {
                        "id": cycle.id,
                        "numero": cycle.numero,
                        "data_inicio": cycle.data_inicio,
                        "data_fim": cycle.data_fim,
                        "status": cycle.status,
                        "valor_total": cycle.valor_total,
                    }
                    for cycle in sorted(
                        current_cycles.values(),
                        key=lambda item: (item.numero, item.id),
                    )
                ]
            elif parcel_items:
                referencia_inicial = min(
                    (
                        _parse_optional_date(item.get("referencia_mes"))
                        or _parse_optional_date(item.get("data_vencimento"))
                        or timezone.localdate()
                    )
                    for item in parcel_items
                )
                referencia_final = max(
                    (
                        _parse_optional_date(item.get("referencia_mes"))
                        or _parse_optional_date(item.get("data_vencimento"))
                        or referencia_inicial
                    )
                    for item in parcel_items
                )
                fallback_cycle_ref = "admin-fallback-cycle-1"
                cycles_payload = [
                    {
                        "id": None,
                        "client_key": fallback_cycle_ref,
                        "numero": 1,
                        "data_inicio": referencia_inicial,
                        "data_fim": referencia_final,
                        "status": Ciclo.Status.ABERTO,
                        "valor_total": sum(
                            (_parse_decimal(item.get("valor")) for item in parcel_items),
                            start=Decimal("0.00"),
                        ),
                    }
                ]
                parcel_items = [
                    {
                        **item,
                        "cycle_ref": item.get("cycle_ref") or item.get("cycle_id") or fallback_cycle_ref,
                    }
                    for item in parcel_items
                ]
            else:
                raise ValidationError("Informe ao menos um ciclo no layout.")

        cycle_refs_from_payload: set[str] = set()
        for raw_cycle in cycles_payload:
            if raw_cycle.get("id") is not None:
                cycle_refs_from_payload.add(str(raw_cycle["id"]))
            if raw_cycle.get("client_key"):
                cycle_refs_from_payload.add(str(raw_cycle["client_key"]))
        single_cycle_ref = (
            next(iter(cycle_refs_from_payload))
            if len(cycle_refs_from_payload) == 1
            else None
        )
        for raw_parcela in parcel_items:
            cycle_ref = raw_parcela.get("cycle_ref") or raw_parcela.get("cycle_id")
            if cycle_ref not in (None, ""):
                continue
            parcela = current_parcelas.get(raw_parcela.get("id"))
            if parcela is not None and str(parcela.ciclo_id) in cycle_refs_from_payload:
                raw_parcela["cycle_ref"] = str(parcela.ciclo_id)
            elif single_cycle_ref is not None:
                raw_parcela["cycle_ref"] = single_cycle_ref

        references_seen: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for raw_parcela in parcel_items:
            if str(raw_parcela.get("status") or "") == Parcela.Status.CANCELADO:
                continue
            referencia = _parse_optional_date(raw_parcela.get("referencia_mes"))
            if referencia is None:
                continue
            references_seen[referencia].append(raw_parcela)
        keep_index_by_reference = {}
        for referencia in references_seen:
            indexed_rows = [
                (index, row)
                for index, row in enumerate(parcel_items)
                if _parse_optional_date(row.get("referencia_mes")) == referencia
                and str(row.get("status") or "") != Parcela.Status.CANCELADO
            ]
            cycle_rows = [
                (index, row)
                for index, row in indexed_rows
                if str(row.get("layout_bucket") or Parcela.LayoutBucket.CYCLE)
                == Parcela.LayoutBucket.CYCLE
            ]
            keep_index_by_reference[referencia] = (
                cycle_rows[-1][0] if cycle_rows else indexed_rows[-1][0]
            )
        duplicate_conflicts = []
        for referencia, rows in sorted(references_seen.items()):
            if len(rows) < 2:
                continue
            preserved_index = keep_index_by_reference[referencia]
            preserved_row = parcel_items[preserved_index]
            duplicate_conflicts.append(
                {
                    "referencia_mes": referencia.isoformat(),
                    "preservada": {
                        "id": preserved_row.get("id"),
                        "numero": int(preserved_row.get("numero") or 0),
                        "cycle_ref": preserved_row.get("cycle_ref") or preserved_row.get("cycle_id"),
                        "layout_bucket": str(
                            preserved_row.get("layout_bucket") or Parcela.LayoutBucket.CYCLE
                        ),
                    },
                    "removidas": [
                        {
                            "id": row.get("id"),
                            "numero": int(row.get("numero") or 0),
                            "cycle_ref": row.get("cycle_ref") or row.get("cycle_id"),
                            "layout_bucket": str(
                                row.get("layout_bucket") or Parcela.LayoutBucket.CYCLE
                            ),
                        }
                        for index, row in enumerate(parcel_items)
                        if _parse_optional_date(row.get("referencia_mes")) == referencia
                        and str(row.get("status") or "") != Parcela.Status.CANCELADO
                        and index != preserved_index
                    ],
                }
            )
        parcel_items = [
            raw_parcela
            for index, raw_parcela in enumerate(parcel_items)
            if (
                str(raw_parcela.get("status") or "") == Parcela.Status.CANCELADO
                or (_parse_optional_date(raw_parcela.get("referencia_mes")) is None)
                or keep_index_by_reference.get(
                    _parse_optional_date(raw_parcela.get("referencia_mes"))
                )
                == index
            )
        ]
        for conflict in duplicate_conflicts:
            operation_warnings.append(
                _warning_payload(
                    code="duplicate_reference_month",
                    contrato=contrato,
                    scope="cycle_layout",
                    competencia=conflict["referencia_mes"],
                    action="review_duplicate_reference",
                    message=(
                        "A competência "
                        f"{date.fromisoformat(conflict['referencia_mes']).strftime('%m/%Y')} "
                        "aparecia em mais de uma parcela."
                    ),
                    details=conflict,
                )
            )
            operation_warnings.append(
                _warning_payload(
                    code="duplicate_reference_normalized",
                    contrato=contrato,
                    scope="cycle_layout",
                    competencia=conflict["referencia_mes"],
                    action="normalized_last_occurrence",
                    message=(
                        "A competência "
                        f"{date.fromisoformat(conflict['referencia_mes']).strftime('%m/%Y')} "
                        "estava duplicada. O sistema preservou a última ocorrência salva."
                    ),
                    details=conflict,
                )
            )

        operation_warnings.extend(
            _build_contract_layout_warnings(
                contrato,
                cycles_payload=cycles_payload,
                parcel_items=parcel_items,
            )
        )

        cycle_ref_map: dict[str, Ciclo] = {}
        touched_cycle_ids: set[int] = set()
        touched_parcela_ids: set[int] = set()
        assigned_numbers_by_cycle: defaultdict[int, set[int]] = defaultdict(set)

        atomic_ctx = transaction.atomic() if wrap_atomic else nullcontext()
        with atomic_ctx:
            # Renumeração temporária: usamos max_numero_existente + 1 + index para garantir
            # que os números temporários sejam únicos globalmente e não colidam com nenhum
            # número já existente. Isso evita violação da constraint (ciclo_id, numero)
            # e mantém os valores dentro do range smallint unsigned (0-65535).
            existing_cycle_list = all_cycles
            if existing_cycle_list:
                max_ciclo_numero = max(c.numero for c in existing_cycle_list)
                for index, ciclo in enumerate(existing_cycle_list):
                    temporary_number = max_ciclo_numero + 1 + index
                    if ciclo.numero != temporary_number:
                        ciclo.numero = temporary_number
                        ciclo.save(update_fields=["numero", "updated_at"])

            existing_parcela_list = all_parcelas
            if existing_parcela_list:
                max_parcela_numero = max(p.numero for p in existing_parcela_list)
                for index, parcela in enumerate(existing_parcela_list):
                    temporary_number = max_parcela_numero + 1 + index
                    if parcela.numero != temporary_number:
                        parcela.numero = temporary_number
                        parcela.save(update_fields=["numero", "updated_at"])

            for raw_cycle in cycles_payload:
                cycle_id = raw_cycle.get("id")
                cycle = current_cycles.get(cycle_id) if cycle_id else None
                if cycle is None:
                    cycle = Ciclo.objects.create(
                        contrato=contrato,
                        numero=int(raw_cycle.get("numero") or 1),
                        data_inicio=_parse_optional_date(raw_cycle.get("data_inicio")) or timezone.localdate(),
                        data_fim=_parse_optional_date(raw_cycle.get("data_fim")) or timezone.localdate(),
                        status=_normalize_cycle_status(raw_cycle.get("status")),
                        valor_total=_parse_decimal(raw_cycle.get("valor_total")),
                    )
                else:
                    cycle.numero = int(raw_cycle.get("numero") or cycle.numero)
                    cycle.data_inicio = _parse_optional_date(raw_cycle.get("data_inicio")) or cycle.data_inicio
                    cycle.data_fim = _parse_optional_date(raw_cycle.get("data_fim")) or cycle.data_fim
                    cycle.status = _normalize_cycle_status(
                        raw_cycle.get("status"),
                        default=cycle.status,
                    )
                    cycle.valor_total = _parse_decimal(
                        raw_cycle.get("valor_total"),
                        cycle.valor_total,
                    )
                    cycle.save()
                touched_cycle_ids.add(cycle.id)
                cycle_ref_map[str(cycle.id)] = cycle
                if raw_cycle.get("client_key"):
                    cycle_ref_map[str(raw_cycle["client_key"])] = cycle

            for raw_parcela in parcel_items:
                parcela_id = raw_parcela.get("id")
                parcela = current_parcelas.get(parcela_id) if parcela_id else None
                cycle_ref = raw_parcela.get("cycle_ref") or raw_parcela.get("cycle_id")
                if cycle_ref in (None, ""):
                    raise ValidationError(
                        {
                            "detail": "Parcela sem ciclo de destino válido.",
                            "parcela": {
                                "id": raw_parcela.get("id"),
                                "cycle_ref": cycle_ref,
                                "referencia_mes": raw_parcela.get("referencia_mes"),
                            },
                        }
                    )
                cycle = cycle_ref_map.get(str(cycle_ref))
                if cycle is None:
                    raise ValidationError(
                        {
                            "detail": "Parcela sem ciclo de destino válido.",
                            "parcela": {
                                "id": raw_parcela.get("id"),
                                "cycle_ref": cycle_ref,
                                "referencia_mes": raw_parcela.get("referencia_mes"),
                            },
                        }
                    )
                requested_number = int(
                    raw_parcela.get("numero")
                    or (parcela.numero if parcela is not None else 1)
                    or 1
                )
                allocated_number = requested_number
                while allocated_number in assigned_numbers_by_cycle[cycle.id]:
                    allocated_number += 1
                assigned_numbers_by_cycle[cycle.id].add(allocated_number)
                if parcela is None:
                    parcela = Parcela.all_objects.create(
                        ciclo=cycle,
                        associado=contrato.associado,
                        numero=allocated_number,
                        referencia_mes=_parse_optional_date(raw_parcela.get("referencia_mes")) or cycle.data_inicio,
                        valor=_parse_decimal(raw_parcela.get("valor")),
                        data_vencimento=_parse_optional_date(raw_parcela.get("data_vencimento")) or cycle.data_inicio,
                        status=str(raw_parcela.get("status") or Parcela.Status.EM_PREVISAO),
                        data_pagamento=_parse_optional_date(raw_parcela.get("data_pagamento")),
                        observacao=str(raw_parcela.get("observacao") or ""),
                        layout_bucket=str(raw_parcela.get("layout_bucket") or Parcela.LayoutBucket.CYCLE),
                    )
                else:
                    parcela.ciclo = cycle
                    parcela.associado = contrato.associado
                    parcela.numero = allocated_number
                    parcela.referencia_mes = _parse_optional_date(raw_parcela.get("referencia_mes")) or parcela.referencia_mes
                    parcela.valor = _parse_decimal(raw_parcela.get("valor"), parcela.valor)
                    parcela.data_vencimento = _parse_optional_date(raw_parcela.get("data_vencimento")) or parcela.data_vencimento
                    parcela.status = str(raw_parcela.get("status") or parcela.status)
                    parcela.data_pagamento = _parse_optional_date(raw_parcela.get("data_pagamento"))
                    parcela.observacao = str(raw_parcela.get("observacao") or "")
                    parcela.layout_bucket = str(raw_parcela.get("layout_bucket") or Parcela.LayoutBucket.CYCLE)
                    parcela.save()
                touched_parcela_ids.add(parcela.id)

            for parcela in current_parcelas.values():
                if parcela.id not in touched_parcela_ids:
                    parcela.soft_delete()

            for ciclo in current_cycles.values():
                if ciclo.id not in touched_cycle_ids:
                    for parcela in Parcela.all_objects.filter(ciclo=ciclo, deleted_at__isnull=True):
                        if parcela.id not in touched_parcela_ids:
                            parcela.soft_delete()
                    ciclo.soft_delete()

            contrato.admin_manual_layout_enabled = True
            contrato.admin_manual_layout_updated_at = timezone.now()
            latest_cycle_payload = max(
                cycles_payload,
                key=lambda item: int(item.get("numero") or 0),
                default=None,
            )
            if (
                latest_cycle_payload is not None
                and is_dedicated_small_value_contract(contrato)
                and _normalize_cycle_status(latest_cycle_payload.get("status"))
                in {
                    Ciclo.Status.APTO_A_RENOVAR,
                    Ciclo.Status.FECHADO,
                }
            ):
                contrato.allow_small_value_renewal = True
            contrato.save(
                update_fields=[
                    "admin_manual_layout_enabled",
                    "admin_manual_layout_updated_at",
                    "allow_small_value_renewal",
                    "updated_at",
                ]
            )

            new_parcela_candidates_by_reference: dict[date, list[Parcela]] = defaultdict(list)
            for parcela in (
                Parcela.all_objects.filter(
                    ciclo__contrato=contrato,
                    deleted_at__isnull=True,
                )
                .exclude(status=Parcela.Status.CANCELADO)
                .order_by("ciclo__numero", "numero", "id")
            ):
                new_parcela_candidates_by_reference[parcela.referencia_mes].append(parcela)
            new_parcela_by_reference = {
                referencia: items[0]
                for referencia, items in new_parcela_candidates_by_reference.items()
                if len(items) == 1
            }
            rebind_financial_links_by_reference(
                old_reference_by_parcela_id=old_reference_by_parcela_id,
                new_parcela_by_reference=new_parcela_by_reference,
            )

        if sync_associado_status:
            sync_associado_mother_status(contrato.associado)
        after_snapshot = AdminOverrideService.build_contract_projection_for_response(
            contrato,
            include_documents=True,
        )
        changes = []
        for cycle in contrato.ciclos.filter(deleted_at__isnull=True).order_by("numero", "id"):
            changes.append(
                {
                    "entity_type": AdminOverrideChange.EntityType.CICLO,
                    "entity_id": cycle.id,
                    "resumo": f"Layout do ciclo {cycle.numero} ajustado",
                    "before_snapshot": {},
                    "after_snapshot": _serialize_ciclo(cycle),
                }
            )
        for parcela in (
            Parcela.all_objects.filter(ciclo__contrato=contrato, deleted_at__isnull=True)
            .exclude(status=Parcela.Status.CANCELADO)
            .order_by("ciclo__numero", "numero", "id")
        ):
            changes.append(
                {
                    "entity_type": AdminOverrideChange.EntityType.PARCELA,
                    "entity_id": parcela.id,
                    "competencia_referencia": parcela.referencia_mes,
                    "resumo": f"Competência {parcela.referencia_mes.strftime('%m/%Y')} movida/atualizada",
                    "before_snapshot": {},
                    "after_snapshot": _serialize_parcela(parcela),
                }
            )
        AdminOverrideService._record_event(
            context=_OverrideContext(associado=contrato.associado, contrato=contrato),
            user=user,
            escopo=AdminOverrideEvent.Scope.CICLOS,
            resumo=f"Layout manual dos ciclos do contrato {contrato.codigo}",
            motivo=motivo,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            changes=changes,
        )
        return contrato, _dedupe_warning_list(operation_warnings)

    @staticmethod
    def revert_event(
        *,
        event: AdminOverrideEvent,
        motivo: str,
        user,
    ) -> AdminOverrideEvent:
        if event.revertida_em is not None:
            raise ValidationError("Esta operação já foi revertida.")
        if not motivo.strip():
            raise ValidationError("O motivo da reversão é obrigatório.")

        with transaction.atomic():
            if event.escopo == AdminOverrideEvent.Scope.ASSOCIADO:
                associado = event.associado
                snapshot = event.before_snapshot.get("associado") or {}
                contract_snapshot = event.before_snapshot.get("contrato") or {}
                esteira_snapshot = event.before_snapshot.get("esteira") or {}
                if snapshot:
                    associado.nome_completo = snapshot.get("nome_completo") or associado.nome_completo
                    associado.rg = snapshot.get("rg") or ""
                    associado.orgao_expedidor = snapshot.get("orgao_expedidor") or ""
                    associado.email = snapshot.get("email") or ""
                    associado.telefone = snapshot.get("telefone") or ""
                    associado.data_nascimento = _parse_optional_date(snapshot.get("data_nascimento"))
                    associado.profissao = snapshot.get("profissao") or ""
                    associado.estado_civil = snapshot.get("estado_civil") or ""
                    associado.orgao_publico = snapshot.get("orgao_publico") or ""
                    associado.matricula_orgao = snapshot.get("matricula_orgao") or ""
                    associado.cargo = snapshot.get("cargo") or ""
                    associado.status = snapshot.get("status") or associado.status
                    associado.observacao = snapshot.get("observacao") or ""
                    associado.agente_responsavel_id = snapshot.get("agente_responsavel_id")
                    if snapshot.get("percentual_repasse") not in (None, ""):
                        associado.auxilio_taxa = _parse_decimal(snapshot.get("percentual_repasse"))

                    endereco = snapshot.get("endereco") or {}
                    associado.cep = str(endereco.get("cep") or "")
                    associado.logradouro = str(endereco.get("endereco") or "")
                    associado.numero = str(endereco.get("numero") or "")
                    associado.complemento = str(endereco.get("complemento") or "")
                    associado.bairro = str(endereco.get("bairro") or "")
                    associado.cidade = str(endereco.get("cidade") or "")
                    associado.uf = str(endereco.get("uf") or "")

                    dados_bancarios = snapshot.get("dados_bancarios") or {}
                    associado.banco = str(dados_bancarios.get("banco") or "")
                    associado.agencia = str(dados_bancarios.get("agencia") or "")
                    associado.conta = str(dados_bancarios.get("conta") or "")
                    associado.tipo_conta = str(dados_bancarios.get("tipo_conta") or "")
                    associado.chave_pix = str(dados_bancarios.get("chave_pix") or "")

                    contato = snapshot.get("contato") or {}
                    associado.telefone = str(contato.get("celular") or associado.telefone)
                    associado.email = str(contato.get("email") or associado.email)
                    associado.orgao_publico = str(contato.get("orgao_publico") or associado.orgao_publico)
                    associado.situacao_servidor = str(contato.get("situacao_servidor") or "")
                    associado.matricula_orgao = str(contato.get("matricula_servidor") or associado.matricula_orgao)
                    associado.save()

                if esteira_snapshot:
                    esteira = getattr(associado, "esteira_item", None)
                    if esteira is None:
                        esteira = EsteiraItem.objects.create(
                            associado=associado,
                            etapa_atual=str(
                                esteira_snapshot.get("etapa_atual")
                                or EsteiraItem.Etapa.ANALISE
                            ),
                            status=str(
                                esteira_snapshot.get("status")
                                or EsteiraItem.Situacao.AGUARDANDO
                            ),
                            prioridade=int(esteira_snapshot.get("prioridade") or 3),
                            observacao=str(esteira_snapshot.get("observacao") or ""),
                        )
                    else:
                        esteira.etapa_atual = (
                            esteira_snapshot.get("etapa_atual") or esteira.etapa_atual
                        )
                        esteira.status = esteira_snapshot.get("status") or esteira.status
                        esteira.prioridade = int(
                            esteira_snapshot.get("prioridade") or esteira.prioridade
                        )
                        esteira.observacao = str(esteira_snapshot.get("observacao") or "")
                    esteira.assumido_em = (
                        parse_datetime(str(esteira_snapshot.get("assumido_em")))
                        if esteira_snapshot.get("assumido_em")
                        else None
                    )
                    esteira.heartbeat_at = (
                        parse_datetime(str(esteira_snapshot.get("heartbeat_at")))
                        if esteira_snapshot.get("heartbeat_at")
                        else None
                    )
                    esteira.concluido_em = (
                        parse_datetime(str(esteira_snapshot.get("concluido_em")))
                        if esteira_snapshot.get("concluido_em")
                        else None
                    )
                    esteira.analista_responsavel_id = esteira_snapshot.get(
                        "analista_responsavel_id"
                    )
                    esteira.coordenador_responsavel_id = esteira_snapshot.get(
                        "coordenador_responsavel_id"
                    )
                    esteira.tesoureiro_responsavel_id = esteira_snapshot.get(
                        "tesoureiro_responsavel_id"
                    )
                    esteira.save()

                if event.contrato_id and contract_snapshot:
                    contrato = event.contrato
                    contrato.codigo = contract_snapshot.get("codigo") or contrato.codigo
                    contrato.status = contract_snapshot.get("status") or contrato.status
                    contrato.valor_bruto = _parse_decimal(contract_snapshot.get("valor_bruto"), contrato.valor_bruto)
                    contrato.valor_liquido = _parse_decimal(contract_snapshot.get("valor_liquido"), contrato.valor_liquido)
                    contrato.valor_mensalidade = _parse_decimal(
                        contract_snapshot.get("valor_mensalidade"),
                        contrato.valor_mensalidade,
                    )
                    contrato.prazo_meses = int(contract_snapshot.get("prazo_meses") or contrato.prazo_meses)
                    contrato.taxa_antecipacao = _parse_decimal(contract_snapshot.get("taxa_antecipacao"), contrato.taxa_antecipacao)
                    contrato.margem_disponivel = _parse_decimal(contract_snapshot.get("margem_disponivel"), contrato.margem_disponivel)
                    contrato.valor_total_antecipacao = _parse_decimal(
                        contract_snapshot.get("valor_total_antecipacao"),
                        contrato.valor_total_antecipacao,
                    )
                    contrato.doacao_associado = _parse_decimal(contract_snapshot.get("doacao_associado"), contrato.doacao_associado)
                    contrato.comissao_agente = _parse_decimal(contract_snapshot.get("comissao_agente"), contrato.comissao_agente)
                    contrato.data_contrato = _parse_optional_date(contract_snapshot.get("data_contrato")) or contrato.data_contrato
                    contrato.data_aprovacao = _parse_optional_date(contract_snapshot.get("data_aprovacao"))
                    contrato.data_primeira_mensalidade = _parse_optional_date(contract_snapshot.get("data_primeira_mensalidade"))
                    contrato.mes_averbacao = _parse_optional_date(contract_snapshot.get("mes_averbacao"))
                    contrato.auxilio_liberado_em = _parse_optional_date(contract_snapshot.get("auxilio_liberado_em"))
                    contrato.agente_id = contract_snapshot.get("agente_id")
                    contrato.admin_manual_layout_enabled = bool(contract_snapshot.get("admin_manual_layout_enabled"))
                    contrato.save()
            elif event.escopo == AdminOverrideEvent.Scope.CONTRATO and event.contrato_id:
                snapshot = event.before_snapshot or {}
                contrato = event.contrato
                contrato.status = snapshot.get("status") or contrato.status
                contrato.valor_bruto = _parse_decimal(snapshot.get("valor_bruto"), contrato.valor_bruto)
                contrato.valor_liquido = _parse_decimal(snapshot.get("valor_liquido"), contrato.valor_liquido)
                contrato.valor_mensalidade = _parse_decimal(snapshot.get("valor_mensalidade"), contrato.valor_mensalidade)
                contrato.taxa_antecipacao = _parse_decimal(snapshot.get("taxa_antecipacao"), contrato.taxa_antecipacao)
                contrato.margem_disponivel = _parse_decimal(snapshot.get("margem_disponivel"), contrato.margem_disponivel)
                contrato.valor_total_antecipacao = _parse_decimal(snapshot.get("valor_total_antecipacao"), contrato.valor_total_antecipacao)
                contrato.doacao_associado = _parse_decimal(snapshot.get("doacao_associado"), contrato.doacao_associado)
                contrato.comissao_agente = _parse_decimal(snapshot.get("comissao_agente"), contrato.comissao_agente)
                contrato.agente_id = snapshot.get("agente_id")
                contrato.contato_web = bool(snapshot.get("contato_web"))
                contrato.termos_web = bool(snapshot.get("termos_web"))
                contrato.data_contrato = _parse_optional_date(snapshot.get("data_contrato")) or contrato.data_contrato
                contrato.data_aprovacao = _parse_optional_date(snapshot.get("data_aprovacao"))
                contrato.data_primeira_mensalidade = _parse_optional_date(snapshot.get("data_primeira_mensalidade"))
                contrato.mes_averbacao = _parse_optional_date(snapshot.get("mes_averbacao"))
                contrato.auxilio_liberado_em = _parse_optional_date(snapshot.get("auxilio_liberado_em"))
                contrato.admin_manual_layout_enabled = bool(snapshot.get("admin_manual_layout_enabled"))
                contrato.save()
            elif event.escopo == AdminOverrideEvent.Scope.CICLOS and event.contrato_id:
                cycles = event.before_snapshot.get("cycles") or []
                unpaid = event.before_snapshot.get("unpaid_months") or []
                movements = event.before_snapshot.get("movimentos_financeiros_avulsos") or []
                parcel_items: list[dict[str, Any]] = []
                for cycle in cycles:
                    cycle_ref = cycle.get("id")
                    for parcela in cycle.get("parcelas", []):
                        parcel_items.append(
                            {
                                **parcela,
                                "cycle_ref": cycle_ref,
                                "layout_bucket": Parcela.LayoutBucket.CYCLE,
                            }
                        )
                fallback_cycle_ref = str(cycles[0]["id"]) if cycles else ""
                for row in unpaid:
                    parcel_items.append(
                        {
                            **row,
                            "cycle_ref": fallback_cycle_ref,
                            "numero": 1,
                            "data_vencimento": row["referencia_mes"],
                            "layout_bucket": Parcela.LayoutBucket.UNPAID,
                        }
                    )
                for row in movements:
                    parcel_items.append(
                        {
                            **row,
                            "cycle_ref": fallback_cycle_ref,
                            "numero": 1,
                            "data_vencimento": row["referencia_mes"],
                            "layout_bucket": Parcela.LayoutBucket.MOVEMENT,
                        }
                    )
                _contrato, _warnings = AdminOverrideService.apply_cycle_layout_override(
                    contrato=event.contrato,
                    payload={
                        "updated_at": event.contrato.updated_at.isoformat() if event.contrato.updated_at else None,
                        "motivo": f"Reversão da operação #{event.id}",
                        "cycles": cycles,
                        "parcelas": parcel_items,
                    },
                    user=user,
                )
            elif event.escopo == AdminOverrideEvent.Scope.REFINANCIAMENTO and event.refinanciamento_id:
                refinanciamento = event.refinanciamento
                snapshot = event.before_snapshot or {}
                refinanciamento.status = snapshot.get("status") or refinanciamento.status
                refinanciamento.competencia_solicitada = (
                    _parse_optional_date(snapshot.get("competencia_solicitada"))
                    or refinanciamento.competencia_solicitada
                )
                refinanciamento.valor_refinanciamento = _parse_decimal(
                    snapshot.get("valor_refinanciamento"),
                    refinanciamento.valor_refinanciamento,
                )
                refinanciamento.repasse_agente = _parse_decimal(
                    snapshot.get("repasse_agente"),
                    refinanciamento.repasse_agente,
                )
                refinanciamento.executado_em = parse_datetime(str(snapshot.get("executado_em"))) if snapshot.get("executado_em") else None
                refinanciamento.data_ativacao_ciclo = parse_datetime(str(snapshot.get("data_ativacao_ciclo"))) if snapshot.get("data_ativacao_ciclo") else None
                refinanciamento.motivo_bloqueio = snapshot.get("motivo_bloqueio") or ""
                refinanciamento.observacao = snapshot.get("observacao") or ""
                refinanciamento.analista_note = snapshot.get("analista_note") or ""
                refinanciamento.coordenador_note = snapshot.get("coordenador_note") or ""
                refinanciamento.reviewed_by_id = snapshot.get("reviewed_by_id")
                refinanciamento.save()
            elif event.escopo == AdminOverrideEvent.Scope.ESTEIRA:
                esteira = getattr(event.associado, "esteira_item", None)
                snapshot = event.before_snapshot or {}
                if esteira is not None:
                    esteira.etapa_atual = snapshot.get("etapa_atual") or esteira.etapa_atual
                    esteira.status = snapshot.get("status") or esteira.status
                    esteira.prioridade = int(snapshot.get("prioridade") or esteira.prioridade)
                    esteira.observacao = snapshot.get("observacao") or ""
                    esteira.analista_responsavel_id = snapshot.get("analista_responsavel_id")
                    esteira.coordenador_responsavel_id = snapshot.get("coordenador_responsavel_id")
                    esteira.tesoureiro_responsavel_id = snapshot.get("tesoureiro_responsavel_id")
                    esteira.save()
            elif event.escopo == AdminOverrideEvent.Scope.DOCUMENTO:
                if event.before_snapshot.get("id"):
                    documento = Documento.all_objects.filter(pk=event.before_snapshot["id"]).first()
                    if documento is not None:
                        documento.deleted_at = None
                        documento.save(update_fields=["deleted_at", "updated_at"])
                if event.after_snapshot.get("id"):
                    current = Documento.objects.filter(pk=event.after_snapshot["id"]).first()
                    if current is not None:
                        current.soft_delete()
            elif event.escopo == AdminOverrideEvent.Scope.COMPROVANTE:
                if event.before_snapshot.get("id"):
                    comprovante = Comprovante.all_objects.filter(pk=event.before_snapshot["id"]).first()
                    if comprovante is not None:
                        comprovante.deleted_at = None
                        comprovante.save(update_fields=["deleted_at", "updated_at"])
                if event.after_snapshot.get("id"):
                    current = Comprovante.objects.filter(pk=event.after_snapshot["id"]).first()
                    if current is not None:
                        current.soft_delete()

            event.revertida_em = timezone.now()
            event.revertida_por = user
            event.motivo_reversao = motivo
            event.save(update_fields=["revertida_em", "revertida_por", "motivo_reversao", "updated_at"])
            return event
